"""Job Tracker Bot — entry point.

Supports:
  python main.py --dry-run              # one scan, print results, no DB/notifications
  python main.py --dry-run --source remotive   # test a single source
  python main.py --dry-run --verbose    # show rejected jobs with reasons
  python main.py --stats                # show database statistics
  python main.py --weekly-digest        # send the weekly NGO digest now
  python main.py --backfill-scores      # re-score all jobs with match_score=0
  python main.py                        # full scheduler mode (APScheduler)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import config
from filters.employment import (
    employment_display_lines,
    persisted_employment_display_lines,
)
from filters.language import language_display_text
from filters.pipeline import (
    MAX_JOBS_PER_COMPANY,
    passes_company_blocklist,
    passes_salary_filter,
    passes_senior_filter,
    rejection_pairs,
    run_filter_pipeline,
)
from models.job import Job
from models.scan import (
    FilterRunSummary,
    SanitizedSourceIssue,
    ScanSummary,
    SourceFetchOutcome,
    SourceFunnelMetrics,
    SourceStatus,
    classify_source_exception,
    sanitize_source_error,
    utc_now,
)
from notifiers.discord_notifier import DiscordNotifier
from notifiers.telegram_notifier import TelegramNotifier
from sources.arbeitnow import ArbeitnowSource
from sources.ats_url_sniffer import append_sniffed_candidates
from sources.ashby import AshbySource
from sources.bamboohr import BambooHRSource
from sources.devex import DevexSource
from sources.eurobrussels import EuroBrusselsSource
from sources.goodjobs import GoodJobsSource
from sources.greenhouse import GreenhouseSource
from sources.himalayas import HimalayasSource
from sources.hours80k import Hours80kSource
from sources.idealist import IdealistSource
from sources.landingjobs import LandingJobsSource
from sources.linkedin import LinkedInSource
from sources.lever import LeverSource
from sources.nofluffjobs import NoFluffJobsSource
from sources.personio import PersonioSource
from sources.jsonld import JsonLdCareerSource
from sources.reliefweb import ReliefWebSource
from sources.remoteok import RemoteOKSource
from sources.remotive import RemotiveSource
from sources.stepstone import StepstoneSource
from sources.techjobsforgood import TechJobsForGoodSource
from sources.themuse import TheMuseSource
from sources.weworkremotely import WeWorkRemotelySource
from sources.workable import WorkableSource
from sources.base import BaseSource
from storage.database import (
    backfill_match_scores,
    filter_unseen,
    get_recent_unnotified,
    get_latest_scan_summary,
    get_latest_source_statuses,
    get_stats,
    get_total_count,
    get_weekly_general_count,
    get_weekly_ngo_jobs,
    init_db,
    mark_notified,
    persist_scan_metrics,
    save_jobs,
)

# ── Logging setup ──────────────────────────────────────────────────────────
logger.remove()  # remove default stderr handler
logger.add(sys.stderr, level=config.LOG_LEVEL, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
log_dir = Path(config.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)
logger.add(config.LOG_FILE, level="DEBUG", rotation="10 MB", retention="7 days")

# Max jobs per company in a single scan to avoid one employer flooding results
_MAX_JOBS_PER_COMPANY = MAX_JOBS_PER_COMPANY

# ── Source registry ────────────────────────────────────────────────────────
ALL_SOURCES = {
    # Direct employer boards (default)
    "greenhouse": GreenhouseSource,
    "ashby": AshbySource,
    "personio": PersonioSource,
    "lever": LeverSource,
    "workable": WorkableSource,
    "jsonld": JsonLdCareerSource,
    # Curated secondary sources with useful candidate-location data (default)
    "remotive": RemotiveSource,
    "arbeitnow": ArbeitnowSource,
    "stepstone": StepstoneSource,
    "himalayas": HimalayasSource,
    # Optional/noisy sources remain addressable via --source but are not part
    # of scheduled scans until their eligibility contracts are trustworthy.
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
    "nofluffjobs": NoFluffJobsSource,
    "landingjobs": LandingJobsSource,
    "themuse": TheMuseSource,
    "bamboohr": BambooHRSource,
}

DEFAULT_SOURCE_NAMES = (
    "greenhouse", "ashby", "personio", "lever", "workable", "jsonld",
    "arbeitnow", "stepstone", "remotive", "himalayas",
    "remoteok", "idealist", "linkedin",
)

def _get_sources(source_name: str | None) -> list:
    """Return source instances to run — all or a single one."""
    if source_name:
        cls = ALL_SOURCES.get(source_name)
        if cls is None:
            logger.error("Unknown source '{}'. Available: {}", source_name, list(ALL_SOURCES.keys()))
            sys.exit(1)
        return [cls()]

    return [ALL_SOURCES[name]() for name in DEFAULT_SOURCE_NAMES]


def _passes_company_blocklist(job: Job) -> bool:
    """Compatibility wrapper for older tests and callers."""

    return passes_company_blocklist(job, config)


def _passes_senior_filter(job: Job) -> bool:
    """Compatibility wrapper for older tests and callers."""

    return passes_senior_filter(job, config)


def _passes_salary_filter(job: Job) -> bool:
    """Compatibility wrapper for older tests and callers."""

    return passes_salary_filter(job, config)


def _apply_filters(
    jobs: list[Job],
    max_age_days: int | None = None,
    verbose: bool = False,
) -> list[Job]:
    """Compatibility wrapper around the extracted global filter pipeline."""

    summary = run_filter_pipeline(
        jobs,
        max_age_days=max_age_days,
        verbose=verbose,
        settings=config,
        max_jobs_per_company=_MAX_JOBS_PER_COMPANY,
    )
    if verbose and summary.verbose_rejections:
        _print_rejections(rejection_pairs(summary))
    return summary.accepted_jobs


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
        for line in employment_display_lines(job):
            print(f"      {line}")
        if job.employment_reasons:
            print(f"      🔎  {'; '.join(job.employment_reasons[:4])}")
        language_line = language_display_text(job, include_evidence=True)
        if language_line:
            print(f"      🗣  {language_line}")
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

    Sources are fetched in small batches (MAX_CONCURRENT_SOURCES at a time).
    Each batch is fetched, then its raw jobs are merged and the batch
    results are freed before the next batch starts.  This keeps peak
    memory well within Docker's 512 MB cgroup limit.
    """

    scan_id = uuid.uuid4().hex
    scan_started_at = utc_now()
    all_jobs: list[Job] = []
    outcomes: list[SourceFetchOutcome] = []
    batch_size = config.MAX_CONCURRENT_SOURCES

    for i in range(0, len(sources), batch_size):
        batch_sources = sources[i : i + batch_size]
        results = await asyncio.gather(
            *[_fetch_source_outcome(source) for source in batch_sources]
        )
        for outcome in results:
            outcomes.append(outcome)
            all_jobs.extend(outcome.jobs)
        del results  # free HTTP response data eagerly

    total_raw = len(all_jobs)
    logger.info("Total raw jobs fetched: {}", total_raw)

    if config.ENABLE_ATS_SNIFFING:
        try:
            appended = append_sniffed_candidates(all_jobs)
            if appended:
                logger.info("ATS sniffing discovered {} new candidate boards", appended)
        except Exception:
            logger.exception("ATS sniffing failed; continuing scan")

    filter_summary = run_filter_pipeline(
        all_jobs,
        max_age_days=max_age_days,
        verbose=verbose,
        settings=config,
        max_jobs_per_company=_MAX_JOBS_PER_COMPANY,
    )
    if verbose and filter_summary.verbose_rejections:
        _print_rejections(rejection_pairs(filter_summary))
    filtered = filter_summary.accepted_jobs
    scan_summary = _build_scan_summary(
        scan_id=scan_id,
        started_at=scan_started_at,
        outcomes=outcomes,
        filter_summary=filter_summary,
    )
    del all_jobs  # free unfiltered list

    if dry_run:
        # Print results and exit — don't touch DB or send notifications
        scan_summary.completed_at = utc_now()
        _publish_scan_health(scan_summary)
        _print_jobs(filtered, explain=verbose)
        return filtered

    # Deduplicate against DB
    await init_db()
    new_jobs = await filter_unseen(filtered)
    for job in new_jobs:
        metrics = scan_summary.sources.get(job.source or "unknown")
        if metrics is not None:
            metrics.unseen_count += 1
    del filtered  # free pre-dedup list

    saved_jobs: list[Job] = []
    if new_jobs:
        saved_jobs = await save_jobs(new_jobs)
        logger.info("{} new jobs saved to database", len(saved_jobs))

        for job in saved_jobs:
            metrics = scan_summary.sources.get(job.source or "unknown")
            if metrics is None:
                continue
            metrics.saved_count += 1
            route = (
                job.notification_tier
                if job.notification_tier in {"immediate", "digest"}
                else "diagnostic"
            )
            metrics.routing_counts[route] += 1

    scan_summary.completed_at = utc_now()
    scan_summary.validate_accounting()
    await persist_scan_metrics(scan_summary)
    source_health = await get_latest_source_statuses()
    _publish_scan_health(scan_summary, source_health)

    if saved_jobs:
        # Only strong matches alert immediately. Borderline matches stay
        # unnotified until the scheduled digest; low matches remain as
        # diagnostics and never notify.
        await _send_notifications(saved_jobs)
    else:
        logger.info("No new jobs this cycle")

    return saved_jobs


async def _fetch_source_outcome(source: object) -> SourceFetchOutcome:
    """Use typed outcomes for real sources and adapt legacy list-only mocks."""

    if isinstance(source, BaseSource):
        try:
            return await source.fetch_outcome()
        except Exception as exc:
            # BaseSource.fetch_outcome is defensive, but retaining this guard
            # keeps a broken implementation isolated from sibling sources.
            name = getattr(source, "name", source.__class__.__name__.lower())
            now = utc_now()
            status = classify_source_exception(exc)
            issue = SanitizedSourceIssue.from_error(exc, status)
            return SourceFetchOutcome(name, [], status, now, now, 0, (issue,))

    name = str(getattr(source, "name", source.__class__.__name__.lower()))
    started_at = utc_now()
    started_clock = time.perf_counter()
    try:
        safe_fetch = getattr(source, "safe_fetch")
        jobs = await safe_fetch()
        if not isinstance(jobs, list):
            raise TypeError("legacy source safe_fetch result is not a list")
        status = SourceStatus.HEALTHY if jobs else SourceStatus.ZERO_RESULTS
        issues = ()
    except Exception as exc:
        status = classify_source_exception(exc)
        issue = SanitizedSourceIssue.from_error(exc, status)
        issues = (issue,)
        jobs = []
        logger.error("[{}] Legacy fetch failed ({}): {}", name, status.value, issue.explanation)
    completed_at = utc_now()
    return SourceFetchOutcome(
        source=name,
        jobs=jobs,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, round((time.perf_counter() - started_clock) * 1000)),
        issues=issues,
    )


def _build_scan_summary(
    *,
    scan_id: str,
    started_at: datetime,
    outcomes: list[SourceFetchOutcome],
    filter_summary: FilterRunSummary,
) -> ScanSummary:
    """Merge fetch outcomes with the counts from the single global filter pass."""

    metrics_by_source: dict[str, SourceFunnelMetrics] = {}
    for outcome in outcomes:
        filtered = filter_summary.per_source.get(outcome.source)
        metrics_by_source[outcome.source] = SourceFunnelMetrics(
            source=outcome.source,
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
            duration_ms=outcome.duration_ms,
            status=outcome.status,
            raw_count=filtered.raw_count if filtered else 0,
            accepted_count=filtered.accepted_count if filtered else 0,
            rejection_counts=(
                dict(filtered.rejection_counts) if filtered else {}
            ),
            issue_count=outcome.issue_count,
            sanitized_error=sanitize_source_error(outcome.sanitized_error),
        )

    # Job.source is the attribution authority. Normally it matches the source
    # instance name; this fallback preserves accounting for legacy adapters.
    for source, filtered in filter_summary.per_source.items():
        if source in metrics_by_source:
            continue
        metrics_by_source[source] = SourceFunnelMetrics(
            source=source,
            started_at=started_at,
            completed_at=utc_now(),
            duration_ms=0,
            status=SourceStatus.HEALTHY,
            raw_count=filtered.raw_count,
            accepted_count=filtered.accepted_count,
            rejection_counts=dict(filtered.rejection_counts),
        )

    summary = ScanSummary(
        scan_id=scan_id,
        started_at=started_at,
        completed_at=utc_now(),
        sources=metrics_by_source,
    )
    summary.validate_accounting()
    return summary


def _publish_scan_health(
    summary: ScanSummary,
    source_health: list[dict] | None = None,
) -> None:
    """Publish only bounded, sanitized counts to the in-process health state."""

    try:
        from health import set_scan_summary

        set_scan_summary(summary.to_health_dict(source_health))
    except Exception:
        logger.exception("Failed to publish scan health summary")


async def _send_notifications(jobs: list[Job]) -> None:
    """Send notifications for new jobs via all configured channels."""
    immediate_jobs = [job for job in jobs if job.notification_tier == "immediate"]
    if not immediate_jobs:
        logger.info(
            "Notification routing: 0 immediate, {} digest, {} diagnostic",
            sum(job.notification_tier == "digest" for job in jobs),
            sum(job.notification_tier == "none" for job in jobs),
        )
        return

    # Discord
    if config.DISCORD_WEBHOOK_URL:
        notifier = DiscordNotifier()
        await notifier.send_jobs(immediate_jobs)
    else:
        logger.warning("Discord webhook URL not configured — skipping notifications")

    # Telegram
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        notifier = TelegramNotifier()
        await notifier.send_jobs(immediate_jobs)
    else:
        logger.debug("Telegram not configured — skipping")

    await mark_notified([job.id for job in immediate_jobs])


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


def _print_jobs(jobs: list[Job], *, explain: bool = False) -> None:
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
        print(f"      🧭  {job.workplace_type} · {job.notification_tier}")
        for line in employment_display_lines(job):
            print(f"      {line}")
        if explain and job.employment_reasons:
            print(f"      🔎  {'; '.join(job.employment_reasons[:4])}")
        language_line = language_display_text(job, include_evidence=explain)
        if language_line:
            print(f"      🗣  {language_line}")
        if job.eligibility_reasons:
            print(f"      ✅  {'; '.join(job.eligibility_reasons)}")
        if job.match_score > 0:
            from filters.match import match_score_bar
            bar = match_score_bar(job.match_score)
            print(f"      📊  {bar}  {job.match_score}% match")
            if job.match_reasons:
                print(f"      🎯  {'; '.join(job.match_reasons)}")
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
    notification_tiers = stats.get("notification_tiers", {})
    top_companies = stats["top_companies"]
    last_fetched = stats["last_fetched_at"]
    source_health = stats.get("source_health", [])

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
    print("  📊  JOB TRACKER — DATABASE STATS")
    print(f"{'='*60}\n")

    print(f"  Total jobs in DB:    {total}")
    print(f"  New in last 24h:     {new_24h}")
    print(f"  NGO / nonprofit:     {ngo_count}")
    print(f"  General:             {total - ngo_count}")
    print(f"  Last scan:           {last_scan_str}")
    print(
        "  Routing:             "
        f"{notification_tiers.get('immediate', 0)} immediate · "
        f"{notification_tiers.get('digest', 0)} digest · "
        f"{notification_tiers.get('none', 0)} diagnostic"
    )

    if sources:
        print(f"\n  {'─'*50}")
        print("  📡  Sources breakdown:")
        for src, count in sources.items():
            bar = "█" * min(count, 40)
            print(f"      {src:<20s} {count:>4d}  {bar}")

    if source_health:
        print(f"\n  {'─'*50}")
        print("  🩺  Latest source health:")
        print(
            "      source             status          issues   raw  accepted  saved  "
            "last usable      last full"
        )
        for item in source_health[:20]:
            source = str(item.get("source", "unknown"))[:18]
            status = str(item.get("status", "unknown_error"))[:15]
            last_usable = item.get("last_usable_at") or "never"
            if last_usable != "never":
                last_usable = str(last_usable).replace("+00:00", "Z")[:16]
            last_full = item.get("last_fully_successful_at") or "never"
            if last_full != "never":
                last_full = str(last_full).replace("+00:00", "Z")[:16]
            print(
                f"      {source:<18s} {status:<15s} "
                f"{int(item.get('issue_count', 0)):>6d} "
                f"{int(item.get('raw', 0)):>5d} {int(item.get('accepted', 0)):>9d} "
                f"{int(item.get('saved', 0)):>6d}  {last_usable:<16s} {last_full}"
            )
            issue_summary = sanitize_source_error(item.get("sanitized_error"))
            if issue_summary:
                print(f"          issue: {issue_summary[:160]}")

    if top_companies:
        print(f"\n  {'─'*50}")
        print("  🏢  Top companies:")
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
        "--explain",
        action="store_true",
        help="Alias for --verbose: show eligibility and role rejection reasons.",
    )
    parser.add_argument(
        "--validate-sources",
        action="store_true",
        help="Validate every configured direct employer board and exit.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show database statistics and exit.",
    )
    parser.add_argument(
        "--weekly-digest",
        action="store_true",
        help="Send the weekly NGO digest immediately and exit.",
    )
    parser.add_argument(
        "--backfill-scores",
        action="store_true",
        help="Re-compute match scores for all jobs with score=0 and exit.",
    )
    parser.add_argument(
        "--zoho-sync",
        action="store_true",
        help="Run one Zoho Mail ingestion cycle and exit. First sync defaults to dry-run.",
    )
    parser.add_argument(
        "--zoho-write",
        action="store_true",
        help="Allow --zoho-sync to write extracted records and advance checkpoints.",
    )
    args = parser.parse_args()
    args.verbose = args.verbose or getattr(args, "explain", False)

    if getattr(args, "validate_sources", False):
        from sources.validate import validate_sources
        failures = asyncio.run(validate_sources())
        raise SystemExit(1 if failures else 0)

    # ── Stats mode — query DB and print summary ───────────────────────
    if args.stats:
        asyncio.run(_show_stats())
        return

    # ── Weekly digest mode — send immediately ─────────────────────────
    if args.weekly_digest:
        logger.info("Sending weekly NGO digest (manual trigger)...")
        asyncio.run(_run_weekly_digest_cli())
        return

    # ── Backfill match scores ─────────────────────────────────────────
    if args.backfill_scores:
        logger.info("Backfilling match scores for existing jobs...")
        asyncio.run(_run_backfill_cli())
        return

    if args.zoho_sync:
        logger.info("Running Zoho Mail ingestion...")
        dry_run = False if args.zoho_write else (True if args.dry_run else None)
        asyncio.run(_run_zoho_sync_cli(dry_run=dry_run))
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

async def _restore_persisted_health_state() -> None:
    """Restore the latest persisted production scan into in-memory health."""

    try:
        persisted = await get_latest_scan_summary()
        if not persisted:
            return
        completed_at = persisted.pop("completed_at", None)
        persisted.pop("scan_id", None)
        from health import set_last_scan, set_scan_summary

        set_scan_summary(persisted)
        if completed_at:
            set_last_scan(datetime.fromisoformat(str(completed_at).replace("Z", "+00:00")))
        logger.info("Restored persisted scan health from {}", completed_at)
    except Exception:
        logger.exception("Failed to restore persisted scan health; continuing startup")

async def _async_main(sources: list) -> None:
    """Run scheduler, Discord bot, health server all in one event loop."""
    # Initialize DB
    await init_db()

    # Restore the most recent completed production scan before the first new
    # startup scan finishes, so /health remains informative during startup.
    await _restore_persisted_health_state()

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

    if config.DAILY_STATUS_ENABLED:
        scheduler.add_job(
            send_daily_status_summary,
            CronTrigger(hour=config.DAILY_STATUS_HOUR, minute=0, timezone=timezone.utc),
            id="daily_status",
            name="Daily Status Summary",
        )
        logger.info("Daily status scheduled: {:02d}:00 UTC", config.DAILY_STATUS_HOUR)

    # Weekly NGO digest — default: Monday 08:00 UTC
    if config.WEEKLY_DIGEST_ENABLED:
        scheduler.add_job(
            send_weekly_ngo_digest,
            CronTrigger(
                day_of_week=config.WEEKLY_DIGEST_DAY,
                hour=config.WEEKLY_DIGEST_HOUR,
                minute=0,
                timezone=timezone.utc,
            ),
            id="weekly_ngo_digest",
            name="Weekly NGO Digest",
        )
        logger.info(
            "Weekly NGO digest scheduled: {}s at {:02d}:00 UTC",
            config.WEEKLY_DIGEST_DAY.upper(), config.WEEKLY_DIGEST_HOUR,
        )

    if config.ZOHO_MAIL_SYNC_ENABLED:
        scheduler.add_job(
            _scheduled_zoho_mail_sync,
            "interval",
            minutes=config.ZOHO_MAIL_SYNC_INTERVAL_MINUTES,
            id="zoho_mail_sync",
            name="Zoho Mail Sync",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            max_instances=1,
        )
        logger.info(
            "Zoho Mail sync scheduled every {} min (dry_run={})",
            config.ZOHO_MAIL_SYNC_INTERVAL_MINUTES,
            config.ZOHO_MAIL_SYNC_DRY_RUN,
        )

    scheduler.start()
    logger.info("Scheduler started — {} sources registered", len(sources))

    # Send startup notification
    await _send_startup_notification(len(sources))

    # Build list of long-running coroutines to gather
    background_tasks: list[asyncio.Task] = []

    # ── Discord bot (optional) ─────────────────────────────────────────
    discord_bot = None
    if config.DISCORD_BOT_TOKEN and config.DISCORD_COMMAND_CHANNEL_ID:
        from discord_bot import JobTrackerBot

        async def _manual_scan_callback():
            sources_list = _get_sources(None)
            return await run_scan(sources_list, dry_run=False)

        channel_id = int(config.DISCORD_COMMAND_CHANNEL_ID)

        def _new_discord_bot() -> JobTrackerBot:
            bot = JobTrackerBot(
                command_channel_id=channel_id,
                scan_callback=_manual_scan_callback,
            )
            scan_job = scheduler.get_job("scan")
            if scan_job:
                bot.set_scan_times(last_scan=None, next_scan=scan_job.next_run_time)
            return bot

        discord_bot = _new_discord_bot()

        async def _run_discord_forever():
            """Keep the Discord bot running, reconnect on failure."""
            nonlocal discord_bot
            while True:
                try:
                    await discord_bot.start(config.DISCORD_BOT_TOKEN)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Discord bot disconnected: {} — reconnecting in 30s", exc)
                    if not discord_bot.is_closed():
                        try:
                            await discord_bot.close()
                        except Exception:
                            pass
                    await asyncio.sleep(30)
                    discord_bot = _new_discord_bot()

        background_tasks.append(asyncio.create_task(
            _run_discord_forever(),
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

    # ── Keep alive: wait for shutdown signal ──────────────────────────
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        # Block here until a signal is received.
        # Scheduler runs jobs in the background via the event loop.
        # Discord bot runs as a background task.
        await stop_event.wait()
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

        # Cancel background tasks (discord bot, etc.)
        for task in background_tasks:
            if not task.done():
                task.cancel()

        if discord_bot and not discord_bot.is_closed():
            try:
                await discord_bot.close()
            except Exception:
                pass

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


async def _run_zoho_sync_cli(*, dry_run: bool | None) -> None:
    from integrations.zoho_mail import ZohoMailIngestionWorker

    worker = ZohoMailIngestionWorker()
    result = await worker.run(dry_run=dry_run)
    mode = "DRY RUN" if result.dry_run else "WRITE"
    print(f"\n{'='*68}")
    print(f"  ZOHO MAIL INGESTION — {mode}")
    print(f"{'='*68}")
    print(f"  Accounts:                {result.accounts}")
    print(f"  Relevant folders:        {result.folders}")
    print(f"  Message summaries seen:  {result.messages_seen}")
    print(f"  Full messages fetched:   {result.full_messages_fetched}")
    print(f"  Extracted records:       {result.extracted_records}")
    print(f"  Review queue records:    {result.review_records}")
    print(f"  Discovery candidates:    {result.discovery_candidates}")
    print(f"  Checkpoint advanced:     {result.checkpoint_advanced}")
    print(f"{'='*68}\n")


async def _scheduled_zoho_mail_sync() -> None:
    from integrations.zoho_mail import ZohoMailIngestionWorker

    logger.info("📬 Scheduled Zoho Mail sync starting...")
    try:
        worker = ZohoMailIngestionWorker()
        result = await worker.run(dry_run=config.ZOHO_MAIL_SYNC_DRY_RUN)
        logger.info(
            "Zoho Mail sync finished: accounts={} folders={} messages={} full={} records={} review={} discovery={} checkpoint={} dry_run={}",
            result.accounts,
            result.folders,
            result.messages_seen,
            result.full_messages_fetched,
            result.extracted_records,
            result.review_records,
            result.discovery_candidates,
            result.checkpoint_advanced,
            result.dry_run,
        )
    except Exception:
        logger.exception("Scheduled Zoho Mail sync failed")


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
                    score = r.get("match_score", 0)
                    workplace = r.get("workplace_type", "unknown")
                    employment_lines = persisted_employment_display_lines(r)
                    employment_text = "".join(
                        f"\n> {line}" for line in employment_lines
                    )
                    job_lines.append(
                        f"{source_icon} **[{r['title']}]({r['url']})**\n"
                        f"> 🏢 {r['company']}  ·  `{r['source']}`  ·  "
                        f"📊 {score}%  ·  🧭 {workplace}{employment_text}"
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


# ── Daily status summary ──────────────────────────────────────────────────

def _daily_status_details(summary: dict) -> tuple[str, str]:
    """Return bounded rejection and degraded-source text for daily status."""

    rejection_counts = summary.get("rejection_counts", {}) or {}
    top_rejections = sorted(
        (
            (str(code).replace("_", " "), int(count))
            for code, count in rejection_counts.items()
            if int(count) > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    rejection_text = (
        " · ".join(f"`{name}` {count}" for name, count in top_rejections)
        if top_rejections
        else "None recorded"
    )

    source_health = summary.get("source_health", {}) or {}
    degraded = sorted(
        (str(name), item)
        for name, item in source_health.items()
        if isinstance(item, dict)
        and item.get("status") not in {"healthy", "zero_results"}
    )
    displayed = degraded[:5]
    degraded_lines = []
    for name, item in displayed:
        status = str(item.get("status") or "unknown_error")[:30]
        issue_count = max(0, int(item.get("issue_count", 0) or 0))
        line = f"`{name[:50]}` {status} · {issue_count} issue(s)"
        issue_summary = sanitize_source_error(item.get("sanitized_error"))
        if issue_summary:
            line += f" · {issue_summary[:100]}"
        degraded_lines.append(line)
    degraded_text = "\n".join(degraded_lines)
    if len(degraded) > 5:
        degraded_text += f" · +{len(degraded) - 5} more"
    return rejection_text[:1000], (degraded_text or "None")[:1000]

async def send_daily_status_summary() -> None:
    """Send one lightweight Discord status embed per day.

    This is intentionally not a job alert. It gives a heartbeat-style summary
    so production can be checked without manually triggering scans.
    """
    if not config.DISCORD_WEBHOOK_URL:
        logger.warning("No Discord webhook configured — skipping daily status")
        return

    try:
        from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
        from health import get_last_scan_time, get_scan_summary

        total = await get_total_count()
        stats = await get_stats()
        summary = get_scan_summary()
        last_scan = get_last_scan_time()

        if last_scan:
            age = datetime.now(timezone.utc) - last_scan
            minutes = int(age.total_seconds() // 60)
            if minutes < 1:
                last_scan_text = "just now"
            elif minutes < 60:
                last_scan_text = f"{minutes}m ago"
            else:
                last_scan_text = f"{minutes // 60}h ago"
            last_scan_text += f" · `{last_scan.isoformat(timespec='seconds')}`"
        else:
            last_scan_text = "No successful scan recorded yet"

        raw = summary.get("raw", 0)
        accepted = summary.get("accepted", summary.get("eligible_role_matches", 0))
        unseen = summary.get("unseen", 0)
        saved = summary.get("saved", 0)
        rejected = summary.get("rejected", 0)
        immediate = summary.get("immediate", 0)
        digest = summary.get("digest", 0)
        diagnostic = summary.get("diagnostic", 0)

        source_counts = summary.get("sources", {}) or {}
        top_sources = sorted(source_counts.items(), key=lambda item: item[1], reverse=True)[:8]
        source_lines = [f"`{name}` {count}" for name, count in top_sources]
        rejection_text, degraded_text = _daily_status_details(summary)

        webhook = AsyncDiscordWebhook(url=config.DISCORD_WEBHOOK_URL, content="")
        embed = DiscordEmbed(
            title="📊 Daily Job Bot Status",
            description="Production heartbeat — no jobs are alerted from this message.",
            color=0x0EA5E9,  # sky blue
        )
        embed.add_embed_field(
            name="Last scan",
            value=last_scan_text,
            inline=False,
        )
        embed.add_embed_field(
            name="Latest scan",
            value=(
                f"`{raw}` raw\n"
                f"`{accepted}` accepted\n"
                f"`{unseen}` unseen\n"
                f"`{saved}` saved\n"
                f"`{rejected}` rejected"
            ),
            inline=True,
        )
        embed.add_embed_field(
            name="Routing",
            value=(
                f"`{immediate}` immediate\n"
                f"`{digest}` digest\n"
                f"`{diagnostic}` diagnostic"
            ),
            inline=True,
        )
        embed.add_embed_field(
            name="Database",
            value=(
                f"`{total}` total tracked\n"
                f"`{stats.get('new_24h', 0)}` new in 24h"
            ),
            inline=True,
        )
        if source_lines:
            embed.add_embed_field(
                name="Top raw sources",
                value=" · ".join(source_lines),
                inline=False,
            )
        embed.add_embed_field(
            name="Top rejection reasons",
            value=rejection_text,
            inline=False,
        )
        embed.add_embed_field(
            name="Failed / partial sources",
            value=degraded_text,
            inline=False,
        )
        embed.set_footer(text="Job Tracker Bot · Daily Status")
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())
        webhook.add_embed(embed)
        await webhook.execute()
        logger.info("📊 Daily status sent to Discord")
    except Exception:
        logger.exception("Daily status summary failed")


# ── Weekly NGO digest ──────────────────────────────────────────────────────

async def send_weekly_ngo_digest() -> None:
    """Build and send a weekly digest of top NGO jobs to Discord.

    Queries the DB for NGO jobs from the last 7 days, sorted by match_score,
    and sends a rich embed to the NGO webhook (or main webhook).
    """
    try:
        ngo_jobs = await get_weekly_ngo_jobs(days=7, limit=20)
        general_count = await get_weekly_general_count(days=7)

        logger.info(
            "📬 Weekly digest: {} NGO jobs, {} general jobs this week",
            len(ngo_jobs), general_count,
        )

        webhook_url = config.DISCORD_WEBHOOK_URL_NGO or config.DISCORD_WEBHOOK_URL
        if not webhook_url:
            logger.warning("No Discord webhook configured — skipping weekly digest")
            return

        from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
        from filters.match import match_score_bar
        from notifiers.discord_notifier import _SOURCE_ICONS, _format_relative_time

        today = datetime.now(timezone.utc)
        week_start = today - timedelta(days=7)
        subtitle = f"Week of {week_start.strftime('%b %d')} – {today.strftime('%b %d, %Y')}"

        if ngo_jobs:
            # Build job lines (up to 10 in the embed)
            job_lines: list[str] = []
            for job in ngo_jobs[:10]:
                source_icon = _SOURCE_ICONS.get(job.get("source", ""), "🌐")
                score = job.get("match_score", 0)
                bar = match_score_bar(score) if score > 0 else ""
                score_str = f"  📊 {bar} {score}%" if score > 0 else ""

                scope = job.get("remote_scope", "")
                scope_str = f"  🌍 {scope}" if scope else ""

                # Parse fetched_at for relative time
                fetched = job.get("fetched_at", "")
                if fetched:
                    try:
                        dt = datetime.fromisoformat(fetched)
                        time_str = _format_relative_time(dt)
                    except (ValueError, TypeError):
                        time_str = ""
                else:
                    time_str = ""

                url = job.get("url", "")
                title = job.get("title", "Unknown")
                company = job.get("company", "Unknown")
                source = job.get("source", "unknown")

                line = (
                    f"{source_icon} **[{title}]({url})**\n"
                    f"> 🏢 {company}  ·  `{source}`{scope_str}{score_str}\n"
                    f"> 🕐 {time_str}" if time_str else
                    f"{source_icon} **[{title}]({url})**\n"
                    f"> 🏢 {company}  ·  `{source}`{scope_str}{score_str}"
                )
                job_lines.append(line)

            description = "\n\n".join(job_lines)
            if len(ngo_jobs) > 10:
                description += f"\n\n*…and {len(ngo_jobs) - 10} more NGO jobs this week*"

            webhook = AsyncDiscordWebhook(url=webhook_url, content="")
            embed = DiscordEmbed(
                title="🗓️  Weekly NGO Jobs Digest",
                description=description,
                color=0x2ECC71,  # green
            )
            embed.add_embed_field(
                name="📊 This Week",
                value=(
                    f"🟢 **{len(ngo_jobs)}** NGO jobs\n"
                    f"🔵 **{general_count}** general jobs"
                ),
                inline=True,
            )
            embed.set_footer(text=f"Job Tracker Bot · {subtitle}")
            embed.set_timestamp(today.isoformat())
            webhook.add_embed(embed)
            await webhook.execute()
            logger.info("📬 Weekly NGO digest sent ({} jobs)", len(ngo_jobs))
        else:
            # Empty state — short informational embed
            webhook = AsyncDiscordWebhook(url=webhook_url, content="")
            embed = DiscordEmbed(
                title="🗓️  Weekly NGO Jobs Digest",
                description=(
                    "No NGO/nonprofit jobs matched your filters this week.\n\n"
                    f"🔵 **{general_count}** general jobs were tracked.\n\n"
                    "💡 *Tip: NGO sources include ReliefWeb, Idealist, "
                    "80,000 Hours, DevEx, and EuroBrussels.*"
                ),
                color=0x95A5A6,  # grey
            )
            embed.set_footer(text=f"Job Tracker Bot · {subtitle}")
            embed.set_timestamp(today.isoformat())
            webhook.add_embed(embed)
            await webhook.execute()
            logger.info("📬 Weekly NGO digest sent (empty — no jobs this week)")

    except Exception:
        logger.exception("Weekly NGO digest failed")


async def _run_weekly_digest_cli() -> None:
    """CLI wrapper: init DB, send digest, exit."""
    await init_db()
    await send_weekly_ngo_digest()


async def _run_backfill_cli() -> None:
    """CLI wrapper: init DB, backfill match scores, print results."""
    await init_db()
    updated = await backfill_match_scores()
    print(f"\n✅ Backfill complete: {updated} jobs scored")


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
