"""Job Tracker Bot — entry point.

Supports:
  python main.py --dry-run              # one scan, print results, no DB/notifications
  python main.py --dry-run --source remotive   # test a single source
  python main.py --dry-run --verbose    # show rejected jobs with reasons
  python main.py --stats                # show database statistics
  python main.py                        # full scheduler mode (APScheduler)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

import config
from filters.language import passes_language_filter
from filters.location import classify_remote_scope, passes_location_filter
from filters.match import compute_match_score
from filters.ngo import classify_ngo
from filters.role import passes_role_filter
from models.job import Job
from notifiers.discord_notifier import DiscordNotifier
from notifiers.telegram_notifier import TelegramNotifier
from sources.arbeitnow import ArbeitnowSource
from sources.devex import DevexSource
from sources.eurobrussels import EuroBrusselsSource
from sources.goodjobs import GoodJobsSource
from sources.himalayas import HimalayasSource
from sources.hours80k import Hours80kSource
from sources.idealist import IdealistSource
from sources.landingjobs import LandingJobsSource
from sources.linkedin import LinkedInSource
from sources.nofluffjobs import NoFluffJobsSource
from sources.reliefweb import ReliefWebSource
from sources.remoteok import RemoteOKSource
from sources.remotive import RemotiveSource
from sources.stepstone import StepstoneSource
from sources.techjobsforgood import TechJobsForGoodSource
from sources.themuse import TheMuseSource
from sources.weworkremotely import WeWorkRemotelySource
from storage.database import (
    filter_unseen,
    get_recent_unnotified,
    get_stats,
    get_total_count,
    init_db,
    mark_notified,
    save_jobs,
)

# ── Logging setup ──────────────────────────────────────────────────────────
logger.remove()  # remove default stderr handler
logger.add(sys.stderr, level=config.LOG_LEVEL, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
log_dir = Path(config.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)
logger.add(config.LOG_FILE, level="DEBUG", rotation="10 MB", retention="7 days")

# Max jobs per company in a single scan to avoid one employer flooding results
_MAX_JOBS_PER_COMPANY = 2

# ── Source registry ────────────────────────────────────────────────────────
ALL_SOURCES = {
    "remotive": RemotiveSource,
    "arbeitnow": ArbeitnowSource,
    "remoteok": RemoteOKSource,
    "weworkremotely": WeWorkRemotelySource,
    "idealist": IdealistSource,
    "reliefweb": ReliefWebSource,
    "techjobsforgood": TechJobsForGoodSource,
    "eurobrussels": EuroBrusselsSource,
    "hours80k": Hours80kSource,
    "goodjobs": GoodJobsSource,
    "devex": DevexSource,
    "linkedin": LinkedInSource,
    "stepstone": StepstoneSource,
    "nofluffjobs": NoFluffJobsSource,
    "himalayas": HimalayasSource,
    "landingjobs": LandingJobsSource,
    "themuse": TheMuseSource,
}

# ── Senior-only title keywords ────────────────────────────────────────────
_SENIOR_ACCEPT = {"senior", "lead", "staff", "principal", "head", "director", "architect"}
_SENIOR_REJECT = {"junior", "mid-level", "mid level", "entry-level", "entry level", "intern"}

# ── Salary parsing regex ──────────────────────────────────────────────────
_SALARY_NUM_RE = re.compile(r"[\d,.]+")


def _get_sources(source_name: str | None) -> list:
    """Return source instances to run — all or a single one."""
    if source_name:
        cls = ALL_SOURCES.get(source_name)
        if cls is None:
            logger.error("Unknown source '{}'. Available: {}", source_name, list(ALL_SOURCES.keys()))
            sys.exit(1)
        return [cls()]

    return [cls() for cls in ALL_SOURCES.values()]


def _passes_company_blocklist(job: Job) -> bool:
    """Return True if the job's company is NOT on the blocklist."""
    if not config.COMPANY_BLOCKLIST:
        return True
    company_lower = job.company.lower().strip()
    for blocked in config.COMPANY_BLOCKLIST:
        if blocked in company_lower:
            return False
    return True


def _passes_senior_filter(job: Job) -> bool:
    """Return True if the job passes the senior-only filter.

    When FILTER_SENIOR_ONLY is enabled:
    - Accept if title contains a senior keyword
    - Accept if title has NO seniority mention at all (assume senior)
    - Reject if title explicitly mentions junior/mid-level
    """
    if not config.FILTER_SENIOR_ONLY:
        return True

    title_lower = job.title.lower()

    # Check for senior keywords (accept)
    for kw in _SENIOR_ACCEPT:
        if kw in title_lower:
            return True

    # Check for junior/mid-level keywords (reject)
    for kw in _SENIOR_REJECT:
        if kw in title_lower:
            return False

    # No seniority mention → assume senior, accept
    return True


def _passes_salary_filter(job: Job) -> bool:
    """Return True if the job passes the minimum salary filter.

    When MIN_SALARY_EUR > 0 and the job has a salary field:
    - Try to parse the first number from the salary string
    - Reject if the parsed value is below MIN_SALARY_EUR
    - Accept if salary can't be parsed (benefit of the doubt)
    """
    if config.MIN_SALARY_EUR <= 0:
        return True
    if not job.salary:
        return True  # no salary listed → accept

    # Try to parse numbers from the salary string
    nums = _SALARY_NUM_RE.findall(job.salary.replace(",", ""))
    if not nums:
        return True  # can't parse → accept

    try:
        # Take the first number as the salary
        salary_val = float(nums[0])
        # If it looks like a monthly salary (< 10000), annualize
        if salary_val < 10000:
            salary_val *= 12
        return salary_val >= config.MIN_SALARY_EUR
    except (ValueError, IndexError):
        return True  # can't parse → accept


def _apply_filters(
    jobs: list[Job],
    max_age_days: int | None = None,
    verbose: bool = False,
) -> list[Job]:
    """Run all filters on a list of jobs. Returns only accepted jobs.

    Also performs:
      - In-memory content-hash dedup
      - Per-company cap (max 2 per scan)
      - Arbeitnow on-site rejection (is_remote=False + germany scope)
      - Arbeitnow unknown→germany scope defaulting

    When *verbose* is True, prints rejection reasons to stdout (for debugging).
    """
    accepted: list[Job] = []
    rejected: list[tuple[Job, str]] = []  # (job, reason) for verbose output
    seen_content_hashes: set[str] = set()
    company_counts: defaultdict[str, int] = defaultdict(int)

    # Sort by posted_at descending so per-company cap keeps most recent
    def _sort_key(j: Job) -> datetime:
        dt = j.posted_at or j.fetched_at
        # Normalize to UTC-aware to avoid naive vs aware comparison errors
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    jobs_sorted = sorted(jobs, key=_sort_key, reverse=True)

    for job in jobs_sorted:
        # Helper to record a rejection reason for verbose output
        def _reject(job: Job, reason: str) -> None:
            if verbose:
                rejected.append((job, reason))

        # 0. In-memory content dedup (title+company+location)
        if job.content_hash in seen_content_hashes:
            logger.debug("Dedup SKIP (in-memory): {}", job.title)
            _reject(job, "dedup (content hash)")
            continue

        # 1. Classify remote scope (enrichment, not a filter)
        #    If the source already set a meaningful scope (e.g. Idealist
        #    pre-classifies from Algolia's remoteZone, RemoteOK pre-parses
        #    location), keep it.
        if job.remote_scope not in ("worldwide", "eu", "germany", "restricted"):
            job.remote_scope = classify_remote_scope(job)

        # 1b. Arbeitnow default: unknown scope → "germany"
        #     Arbeitnow is a Germany-focused board, safe assumption.
        if job.source == "arbeitnow" and job.remote_scope == "unknown":
            job.remote_scope = "germany"

        # 1c. Remote-only board default: unknown scope → "worldwide"
        #     For WeWorkRemotely and Remotive, if scope is still unknown
        #     after classification, they're remote-only boards so benefit
        #     of the doubt → worldwide.
        if job.source in ("weworkremotely", "remotive") and job.remote_scope == "unknown":
            job.remote_scope = "worldwide"

        # 1d. Impact boards default: unknown scope → "worldwide"
        #     80,000 Hours and Idealist are impact boards with often
        #     worldwide-remote or EU-accessible jobs.
        if job.source in ("hours80k", "idealist") and job.remote_scope == "unknown":
            job.remote_scope = "worldwide"

        # 1e. RemoteOK: unknown scope → "worldwide"
        #     RemoteOK is a remote-only board — if scope is unknown,
        #     default to worldwide (benefit of the doubt).
        if job.source == "remoteok" and job.remote_scope == "unknown":
            job.remote_scope = "worldwide"

        # 1f. Arbeitnow on-site rejection: if the API says is_remote=False
        #     and the scope is "germany", the job is on-site only — reject.
        if job.source == "arbeitnow" and not job.is_remote and job.remote_scope == "germany":
            logger.debug("Location REJECT (arbeitnow on-site): {}", job.title)
            _reject(job, "location: arbeitnow on-site (germany, not remote)")
            continue

        # 1g. Company blocklist — checked BEFORE all other filters
        if not _passes_company_blocklist(job):
            logger.info("[{}] Rejected: {} at {} (company blocklist)", job.source, job.title, job.company)
            _reject(job, f"company blocklist: '{job.company}'")
            continue

        # 2. Location filter
        if not passes_location_filter(job):
            _reject(job, f"location: scope={job.remote_scope}, loc='{job.location}'")
            continue

        # 3. Role filter
        if not passes_role_filter(job):
            _reject(job, f"role: no dev keyword in title '{job.title}'")
            continue

        # 4. Language filter
        if not passes_language_filter(job):
            _reject(job, "language: non-English content detected")
            continue

        # 4c. Senior-only filter (optional, off by default)
        if not _passes_senior_filter(job):
            _reject(job, f"senior filter: title '{job.title}' has junior/mid-level")
            continue

        # 4d. Salary filter (optional, off by default)
        if not _passes_salary_filter(job):
            _reject(job, f"salary filter: '{job.salary}' below min {config.MIN_SALARY_EUR}")
            continue

        # 5. Recency filter — reject jobs older than max_age_days
        #    Per-source override: check SOURCE_MAX_AGE_DAYS first
        source_max = config.SOURCE_MAX_AGE_DAYS.get(job.source)
        if source_max is not None:
            effective_max_age = source_max
        elif max_age_days is not None:
            effective_max_age = max_age_days
        else:
            effective_max_age = config.MAX_JOB_AGE_DAYS
        if job.posted_at is not None:
            posted = job.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - posted
            if age > timedelta(days=effective_max_age):
                age_days = age.total_seconds() / 86400
                logger.debug(
                    "Recency REJECT ({:.0f}d old, max {}d): {}",
                    age_days, effective_max_age, job.title,
                )
                _reject(job, f"recency: {age_days:.0f}d old (max {effective_max_age}d)")
                continue

        # 6. NGO classification (enrichment — never rejects)
        classify_ngo(job)

        # 6b. Match score (enrichment — never rejects)
        try:
            job.match_score = compute_match_score(job)
        except Exception as exc:
            logger.warning("Match scoring failed for '{}': {} — defaulting to 0", job.title, exc)
            job.match_score = 0

        # 7. Per-company cap
        company_key = job.company.lower().strip()
        if company_counts[company_key] >= _MAX_JOBS_PER_COMPANY:
            logger.warning(
                "Company cap ({}) reached for '{}' — skipping: {}",
                _MAX_JOBS_PER_COMPANY, job.company, job.title,
            )
            _reject(job, f"company cap: already {_MAX_JOBS_PER_COMPANY} from '{job.company}'")
            continue

        company_counts[company_key] += 1
        seen_content_hashes.add(job.content_hash)
        accepted.append(job)

    logger.info("Filters: {} in → {} accepted", len(jobs), len(accepted))
    logger.debug(
        "[match] {} jobs accepted, {} with match_score set",
        len(accepted), sum(1 for j in accepted if j.match_score is not None),
    )

    # Sort by match_score DESC so highest-match jobs appear first
    accepted.sort(key=lambda j: j.match_score, reverse=True)

    # Print verbose rejection table when requested
    if verbose and rejected:
        _print_rejections(rejected)

    return accepted


def _print_rejections(rejected: list[tuple[Job, str]]) -> None:
    """Print a human-readable table of rejected jobs with reasons."""
    print(f"\n{'='*78}")
    print(f"  REJECTED JOBS: {len(rejected)} total")
    print(f"{'='*78}\n")

    # Group by rejection reason category
    by_reason: defaultdict[str, list[Job]] = defaultdict(list)
    for job, reason in rejected:
        category = reason.split(":")[0].strip()
        by_reason[category].append(job)

    for category, count in sorted(
        ((k, len(v)) for k, v in by_reason.items()),
        key=lambda x: -x[1],
    ):
        print(f"  ── {category.upper()} ({count}) {'─'*50}")

    print()

    for i, (job, reason) in enumerate(rejected, 1):
        age_str = _format_age(job.posted_at)
        print(f"  ❌ [{i}] {job.title}")
        print(f"      🏢  {job.company}")
        print(f"      📍  {job.location} (scope={job.remote_scope or 'unknown'})")
        print(f"      📅  {age_str}  |  🌍  {job.source}")
        print(f"      ⛔  Reason: {reason}")
        print()


async def run_scan(
    sources: list,
    dry_run: bool = False,
    max_age_days: int | None = None,
    verbose: bool = False,
) -> list[Job]:
    """Fetch from all sources, filter, deduplicate, and optionally persist.

    Respects MAX_CONCURRENT_SOURCES to limit peak memory usage.
    """

    all_jobs: list[Job] = []

    # Fetch from all sources with concurrency limit
    max_concurrent = config.MAX_CONCURRENT_SOURCES
    if max_concurrent >= len(sources):
        # All at once
        fetch_tasks = [src.safe_fetch() for src in sources]
        results = await asyncio.gather(*fetch_tasks)
        for batch in results:
            all_jobs.extend(batch)
    else:
        # Run in batches
        for i in range(0, len(sources), max_concurrent):
            batch_sources = sources[i : i + max_concurrent]
            fetch_tasks = [src.safe_fetch() for src in batch_sources]
            results = await asyncio.gather(*fetch_tasks)
            for batch in results:
                all_jobs.extend(batch)

    logger.info("Total raw jobs fetched: {}", len(all_jobs))

    # Apply filters
    filtered = _apply_filters(all_jobs, max_age_days=max_age_days, verbose=verbose)

    if dry_run:
        # Print results and exit — don't touch DB or send notifications
        _print_jobs(filtered)
        return filtered

    # Deduplicate against DB
    await init_db()
    new_jobs = await filter_unseen(filtered)

    if new_jobs:
        await save_jobs(new_jobs)
        logger.info("{} new jobs saved to database", len(new_jobs))

        # Send Discord notifications
        await _send_notifications(new_jobs)
    else:
        logger.info("No new jobs this cycle")

    return new_jobs


async def _send_notifications(jobs: list[Job]) -> None:
    """Send notifications for new jobs via all configured channels."""
    # Discord
    if config.DISCORD_WEBHOOK_URL:
        notifier = DiscordNotifier()
        await notifier.send_jobs(jobs)
    else:
        logger.warning("Discord webhook URL not configured — skipping notifications")

    # Telegram
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        notifier = TelegramNotifier()
        await notifier.send_jobs(jobs)
    else:
        logger.debug("Telegram not configured — skipping")


def _format_age(posted_at: datetime | None) -> str:
    """Human-readable age string for dry-run display."""
    if posted_at is None:
        return "age unknown"
    dt = posted_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days < 0:
        return "posted today"
    if days == 0:
        hours = int(delta.total_seconds() // 3600)
        if hours == 0:
            return "just now"
        return f"{hours}h ago"
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


def _print_jobs(jobs: list[Job]) -> None:
    """Pretty-print jobs to stdout (for --dry-run)."""
    if not jobs:
        print("\n  No jobs matched your filters.\n")
        return

    ngo_jobs = [j for j in jobs if j.is_ngo]
    general_jobs = [j for j in jobs if not j.is_ngo]

    print(f"\n{'='*70}")
    print(f"  DRY RUN RESULTS: {len(jobs)} jobs matched")
    print(f"  🟢 NGO/nonprofit: {len(ngo_jobs)}  |  🔵 General: {len(general_jobs)}")
    print(f"{'='*70}\n")

    for i, job in enumerate(jobs, 1):
        icon = "🟢" if job.is_ngo else "🔵"
        age_str = _format_age(job.posted_at)
        print(f"  {icon} [{i}] {job.title}")
        print(f"      🏢  {job.company}")
        print(f"      📍  {job.location} ({job.remote_scope or 'unknown'})")
        if job.match_score > 0:
            from filters.match import match_score_bar
            bar = match_score_bar(job.match_score)
            print(f"      📊  {bar}  {job.match_score}% match")
        if job.salary:
            print(f"      💰  {job.salary}")
        if job.tags:
            print(f"      🏷️   {', '.join(job.tags[:5])}")
        print(f"      🌍  Source: {job.source}  |  📅  {age_str}")
        print(f"      🔗  {job.url}")
        print()


async def _show_stats() -> None:
    """Query the database and print a summary dashboard."""
    await init_db()
    stats = await get_stats()

    total = stats["total"]
    ngo_count = stats["ngo_count"]
    new_24h = stats["new_24h"]
    sources = stats["sources"]
    top_companies = stats["top_companies"]
    last_fetched = stats["last_fetched_at"]

    # Last scan age
    if last_fetched:
        dt = last_fetched
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            last_scan_str = "just now"
        elif minutes < 60:
            last_scan_str = f"{minutes} minutes ago"
        elif minutes < 1440:
            hours = minutes // 60
            last_scan_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = minutes // 1440
            last_scan_str = f"{days} day{'s' if days != 1 else ''} ago"
    else:
        last_scan_str = "never"

    print(f"\n{'='*60}")
    print(f"  📊  JOB TRACKER — DATABASE STATS")
    print(f"{'='*60}\n")

    print(f"  Total jobs in DB:    {total}")
    print(f"  New in last 24h:     {new_24h}")
    print(f"  NGO / nonprofit:     {ngo_count}")
    print(f"  General:             {total - ngo_count}")
    print(f"  Last scan:           {last_scan_str}")

    if sources:
        print(f"\n  {'─'*50}")
        print(f"  📡  Sources breakdown:")
        for src, count in sources.items():
            bar = "█" * min(count, 40)
            print(f"      {src:<20s} {count:>4d}  {bar}")

    if top_companies:
        print(f"\n  {'─'*50}")
        print(f"  🏢  Top companies:")
        for company, count in top_companies:
            name = company if len(company) <= 35 else company[:32] + "..."
            print(f"      {name:<35s} ({count})")

    print(f"\n{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Job Tracker Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run one scan cycle and print results without saving to DB or sending notifications.",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help=f"Test a single source. Options: {', '.join(ALL_SOURCES.keys())}",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=None,
        metavar="DAYS",
        help=f"Max job age in days (default: {config.MAX_JOB_AGE_DAYS} from MAX_JOB_AGE_DAYS env var).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show rejected jobs with reasons during --dry-run (debug mode).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show database statistics and exit.",
    )
    args = parser.parse_args()

    # ── Stats mode — query DB and print summary ───────────────────────
    if args.stats:
        asyncio.run(_show_stats())
        return

    sources = _get_sources(args.source)
    source_names = [s.name for s in sources]

    if args.dry_run:
        # One-shot scan — no scheduler
        max_age = args.max_age  # None means use config default
        logger.info(
            "Starting DRY RUN scan — sources: {}, max age: {}d{}",
            source_names, max_age or config.MAX_JOB_AGE_DAYS,
            " (verbose)" if args.verbose else "",
        )
        asyncio.run(run_scan(sources, dry_run=True, max_age_days=max_age, verbose=args.verbose))
        return

    # ── Full scheduler mode ────────────────────────────────────────────
    logger.info("Starting Job Tracker Bot in scheduler mode")
    logger.info(
        "Scan every {} min | Digest every {} h | Health check every 1 h",
        config.SCAN_INTERVAL_MINUTES,
        config.DIGEST_INTERVAL_HOURS,
    )
    if config.COMPANY_BLOCKLIST:
        logger.info("Company blocklist active: {}", config.COMPANY_BLOCKLIST)

    asyncio.run(_async_main(sources))


# ── Async entry point — single event loop for everything ────────────────

async def _async_main(sources: list) -> None:
    """Run scheduler, Discord bot, health server all in one event loop."""
    # Initialize DB
    await init_db()

    # Start health HTTP server
    health_runner = None
    try:
        from health import start_health_server, set_jobs_tracked
        health_runner = await start_health_server()
        total = await get_total_count()
        set_jobs_tracked(total)
    except Exception:
        logger.exception("Failed to start health server — continuing without it")

    # Set up APScheduler (no event_loop param — uses running loop automatically)
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        _scheduled_scan,
        "interval",
        minutes=config.SCAN_INTERVAL_MINUTES,
        id="scan",
        name="Job Scan",
        next_run_time=datetime.now(timezone.utc),  # run immediately on start
    )

    scheduler.add_job(
        _scheduled_digest,
        "interval",
        hours=config.DIGEST_INTERVAL_HOURS,
        id="digest",
        name="Digest Summary",
    )

    scheduler.add_job(
        _scheduled_health_check,
        "interval",
        hours=1,
        id="health",
        name="Health Check",
    )

    scheduler.start()
    logger.info("Scheduler started — {} sources registered", len(sources))

    # Send startup notification
    await _send_startup_notification(len(sources))

    # Build list of long-running coroutines to gather
    tasks: list[asyncio.Task] = []

    # ── Discord bot (optional) ─────────────────────────────────────────
    discord_bot = None
    if config.DISCORD_BOT_TOKEN and config.DISCORD_COMMAND_CHANNEL_ID:
        from discord_bot import JobTrackerBot

        async def _manual_scan_callback():
            sources_list = _get_sources(None)
            return await run_scan(sources_list, dry_run=False)

        channel_id = int(config.DISCORD_COMMAND_CHANNEL_ID)
        discord_bot = JobTrackerBot(
            command_channel_id=channel_id,
            scan_callback=_manual_scan_callback,
        )

        scan_job = scheduler.get_job("scan")
        if scan_job:
            discord_bot.set_scan_times(last_scan=None, next_scan=scan_job.next_run_time)

        tasks.append(asyncio.create_task(
            discord_bot.start(config.DISCORD_BOT_TOKEN),
            name="discord-bot",
        ))
        logger.info("Discord bot starting (channel: {})", config.DISCORD_COMMAND_CHANNEL_ID)
    else:
        logger.info("Discord bot not configured (set DISCORD_BOT_TOKEN and DISCORD_COMMAND_CHANNEL_ID)")

    # ── Telegram bot (optional) ────────────────────────────────────────
    telegram_app = None
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            from notifiers.telegram_notifier import TelegramNotifier
            from storage.database import get_stats as _tg_get_stats

            tg_notifier = TelegramNotifier()

            async def _tg_scan_callback():
                sources_list = _get_sources(None)
                return await run_scan(sources_list, dry_run=False)

            async def _tg_stats_callback():
                await init_db()
                return await _tg_get_stats()

            telegram_app = tg_notifier.build_application(
                scan_callback=_tg_scan_callback,
                stats_callback=_tg_stats_callback,
            )

            await tg_notifier.register_commands()
            await telegram_app.initialize()
            await telegram_app.updater.start_polling(drop_pending_updates=True)
            await telegram_app.start()
            logger.info("Telegram bot started with /commands support")
        except Exception:
            logger.exception("Failed to start Telegram bot — continuing without it")
            telegram_app = None
    else:
        logger.info("Telegram bot not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")

    # ── Keep alive: wait forever (scheduler runs in background) ────────
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Wait for stop signal
    tasks.append(asyncio.create_task(stop_event.wait(), name="keepalive"))

    try:
        # asyncio.gather: if any task raises, others keep running
        # We wait until the stop_event task completes (signal received)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check if stop_event was set or if something crashed
        for task in done:
            if task.get_name() != "keepalive" and task.exception():
                logger.error("Task {} crashed: {}", task.get_name(), task.exception())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        logger.exception("Unhandled exception — bot is crashing")
        try:
            await _send_crash_notification(exc)
        except Exception:
            logger.exception("Failed to send crash notification")
    finally:
        # ── Graceful shutdown ──────────────────────────────────────────
        logger.info("Shutting down...")

        if scheduler.running:
            scheduler.shutdown(wait=False)

        # Cancel pending tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        if discord_bot and not discord_bot.is_closed():
            await discord_bot.close()

        if telegram_app:
            try:
                await telegram_app.updater.stop()
                await telegram_app.stop()
                await telegram_app.shutdown()
            except Exception:
                pass

        if health_runner:
            await health_runner.cleanup()

        logger.info("Job Tracker Bot stopped")


# ── Scheduled tasks ────────────────────────────────────────────────────────

async def _scheduled_scan() -> None:
    """Scheduled scan task — runs all sources."""
    from health import is_paused
    if is_paused():
        logger.info("⏸️ Scanning is paused — skipping scheduled scan")
        return

    logger.info("⏰ Scheduled scan starting...")
    sources = _get_sources(None)
    try:
        await run_scan(sources, dry_run=False)
        # Update health endpoint
        try:
            from health import set_last_scan, set_jobs_tracked, set_next_scan_seconds
            set_last_scan(datetime.now(timezone.utc))
            total = await get_total_count()
            set_jobs_tracked(total)
            set_next_scan_seconds(config.SCAN_INTERVAL_MINUTES * 60)
        except Exception:
            pass
    except Exception:
        logger.exception("Scheduled scan failed")


async def _scheduled_digest() -> None:
    """Send a digest summary of recent unnotified jobs.

    Only sends if there are jobs to show OR if no scan has run
    successfully in the last 2 hours (health alert).
    """
    try:
        recent = await get_recent_unnotified(hours=config.DIGEST_INTERVAL_HOURS)
        total = await get_total_count()

        # Check if any scan ran in the last 2 hours
        no_recent_scan = False
        try:
            from health import _last_scan_time
            if _last_scan_time is not None:
                scan_age = datetime.now(timezone.utc) - _last_scan_time
                if scan_age > timedelta(hours=2):
                    no_recent_scan = True
            else:
                no_recent_scan = True
        except ImportError:
            pass

        if recent:
            logger.info(
                "📋 Digest: {} unnotified jobs in last {} hours (total in DB: {})",
                len(recent), config.DIGEST_INTERVAL_HOURS, total,
            )
            # Send digest via Discord as a modern summary embed
            if config.DISCORD_WEBHOOK_URL:
                from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

                job_lines = []
                for r in recent[:15]:
                    source_icon = {
                        "remotive": "🟣", "arbeitnow": "🔴", "remoteok": "🟠",
                        "reliefweb": "🔵", "hours80k": "⚫", "goodjobs": "🟢",
                        "devex": "🔴", "eurobrussels": "🔵",
                    }.get(r.get("source", ""), "🌐")
                    job_lines.append(
                        f"{source_icon} **{r['title']}**\n"
                        f"> 🏢 {r['company']}  ·  `{r['source']}`"
                    )

                description = "\n\n".join(job_lines)
                if len(recent) > 15:
                    description += f"\n\n*…and {len(recent) - 15} more*"

                webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
                embed = DiscordEmbed(
                    title=f"📋  Digest — {len(recent)} jobs in the last {config.DIGEST_INTERVAL_HOURS}h",
                    description=description,
                    color=0x8B5CF6,  # violet for digest
                )
                embed.add_embed_field(
                    name="📊 Database",
                    value=f"`{total}` total jobs tracked",
                    inline=True,
                )
                embed.set_footer(text="Job Tracker Bot · Periodic Digest")
                embed.set_timestamp(datetime.now(timezone.utc).isoformat())
                webhook.add_embed(embed)
                await webhook.execute()
                logger.info("📋 Digest sent to Discord")

                # Mark digest jobs as notified so they don't repeat
                digest_job_ids = [r["id"] for r in recent if r.get("id")]
                await mark_notified(digest_job_ids)
        elif no_recent_scan:
            # Health alert — no scan ran recently and no new jobs
            logger.warning("📋 Digest: no scans in last 2 hours — health alert")
            if config.DISCORD_WEBHOOK_URL:
                from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

                webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
                embed = DiscordEmbed(
                    title="⚠️  Health Alert — No recent scans",
                    description=(
                        "No scan has completed successfully in the last 2 hours.\n"
                        "The bot may be experiencing issues."
                    ),
                    color=0xEF4444,  # red
                )
                embed.add_embed_field(
                    name="📊 Database",
                    value=f"`{total}` total jobs tracked",
                    inline=True,
                )
                embed.set_footer(text="Job Tracker Bot · Health Alert")
                embed.set_timestamp(datetime.now(timezone.utc).isoformat())
                webhook.add_embed(embed)
                await webhook.execute()
        else:
            logger.info(
                "📋 Digest: no unnotified jobs in last {} hours (total in DB: {})",
                config.DIGEST_INTERVAL_HOURS, total,
            )
    except Exception:
        logger.exception("Digest task failed")


async def _scheduled_health_check() -> None:
    """Log a health-check message and update health endpoint."""
    try:
        total = await get_total_count()
        logger.info("💚 Health check — bot is alive, {} jobs tracked so far", total)
        try:
            from health import set_jobs_tracked
            set_jobs_tracked(total)
        except Exception:
            pass
    except Exception:
        logger.exception("Health check failed")


# ── Startup / crash notifications ──────────────────────────────────────────

async def _send_startup_notification(source_count: int) -> None:
    """Send a Discord embed when the bot starts."""
    if not config.DISCORD_WEBHOOK_URL:
        return

    try:
        from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

        webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
        embed = DiscordEmbed(
            title="🤖  Job Tracker Bot started",
            description=(
                f"📡 Monitoring **{source_count}** sources\n"
                f"⏰ Next scan in ~1 minute\n"
                f"🖥️ Server: Oracle Cloud Frankfurt"
            ),
            color=0x10B981,  # emerald green
        )
        if config.COMPANY_BLOCKLIST:
            embed.add_embed_field(
                name="🚫 Company blocklist",
                value=", ".join(f"`{c}`" for c in config.COMPANY_BLOCKLIST),
                inline=False,
            )
        embed.set_footer(text="Job Tracker Bot")
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())
        webhook.add_embed(embed)
        await webhook.execute()
        logger.info("Startup notification sent to Discord")
    except Exception:
        logger.exception("Failed to send startup notification")


async def _send_crash_notification(exc: Exception) -> None:
    """Send a Discord alert when the bot crashes."""
    if not config.DISCORD_WEBHOOK_URL:
        return

    try:
        from discord_webhook import AsyncDiscordWebhook, DiscordEmbed

        error_msg = str(exc)[:500] if exc else "Unknown error"

        webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
        embed = DiscordEmbed(
            title="⚠️  Job Tracker Bot crashed",
            description=(
                f"**Error:** `{error_msg}`\n\n"
                "The bot will restart automatically via Docker."
            ),
            color=0xEF4444,  # red
        )
        embed.set_footer(text="Job Tracker Bot · Crash Alert")
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())
        webhook.add_embed(embed)
        await webhook.execute()
        logger.info("Crash notification sent to Discord")
    except Exception:
        logger.exception("Failed to send crash notification")


if __name__ == "__main__":
    main()
