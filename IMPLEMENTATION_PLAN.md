# Job Tracker Bot — Coverage and Recall Improvement Plan

**Repository:** `saqibroy/jobs-tracker-bot`  
**Primary deployment:** Oracle Cloud free-tier VM  
**Hard runtime limit:** 512 MB container memory  
**Implementation style:** DRY, KISS, YAGNI; small reviewable phases  
**Status:** Ready for Codex planning review

---

## 1. Goal

Increase the number of relevant jobs found for a Berlin-based full-stack/frontend developer without making the production bot unstable or exceeding the 512 MB memory limit.

The improved bot must:

1. Explain where jobs disappear in the pipeline.
2. Support full-time, part-time, fixed-term, contract-employment, and freelance opportunities.
3. Stop rejecting useful German-language postings merely because the posting is written in German.
4. Keep strict Germany/Berlin work-location eligibility.
5. Route strong, borderline, exploratory, and freelance-permission-required opportunities separately.
6. Run different source groups at appropriate intervals.
7. Enable useful existing lightweight sources before adding new scrapers.
8. Ingest job-alert emails from major platforms instead of relying on fragile browser scraping.
9. Keep the existing SQLite, Docker, Discord, Telegram, Zoho, and Oracle deployment architecture.
10. Remain observable, testable, and safe to deploy.

---

## 2. Non-goals

Do not do these unless a later phase explicitly changes this plan:

- Do not migrate SQLite to PostgreSQL.
- Do not introduce Redis, Celery, Kafka, RabbitMQ, Elasticsearch, or another service.
- Do not add a web dashboard.
- Do not add Playwright or Chromium to production.
- Do not build a distributed Raspberry Pi worker.
- Do not scrape LinkedIn or Indeed with authenticated browser automation.
- Do not rewrite the application into another framework or language.
- Do not add an ORM.
- Do not implement speculative abstractions for future services.
- Do not change notification credentials or commit secrets.
- Do not make several phases in one unreviewable commit.

---

## 3. Current constraints and invariants

- Production uses Docker Compose with a 512 MB memory limit.
- Production must continue to expose `GET /health`.
- Existing job URL and content-hash deduplication must remain effective.
- One failed source must not crash the scan.
- Germany/Berlin eligibility remains a hard gate:
  - Remote roles must explicitly allow Germany, Europe/EEA/EMEA, or worldwide.
  - Hybrid/on-site roles must be in Berlin.
- Existing direct ATS sources must keep working.
- Existing Zoho application-email ingestion must keep working.
- `.env`, OAuth tokens, private mail data, logs, and SQLite data must remain untracked.
- Avoid new production dependencies unless the standard library and existing packages cannot solve the problem.
- Every schema change requires a backward-compatible SQLite migration and tests.
- Every phase must update this file before it is marked complete.

---

## 4. Working process

For every phase:

1. Inspect the relevant existing code and tests.
2. Write or update tests before or alongside implementation.
3. Make the smallest coherent implementation.
4. Run targeted tests.
5. Run the complete test suite used by CI.
6. Run a Docker build.
7. Run a representative dry scan.
8. Record behavior and memory observations.
9. Update the phase checklist and progress log.
10. Commit the phase separately.

Do not start the next phase until the current phase passes its definition of done.

---

# Phase 0 — Baseline and safety snapshot

## Objective

Create a trustworthy before-state and reconcile the documented test command with the real CI workflow.

## Tasks

- [x] Confirm the current feature branch/worktree (`v-2`).
- [x] Build a tagged production image from the current repository.
- [x] Record the branch, commit, Python version, installed dependency versions, and image size.
- [x] Run the blocking test commands in the Docker Python 3.11 environment:
  - `python -m pytest tests/v2 -q`
  - `python -m pytest -q --timeout=30`
- [x] Mount the complete historical `tests/` directory read-only and run the non-blocking compatibility diagnostic:
  - `python -m pytest tests -q --timeout=30`
  - The mount is required because `.dockerignore` includes `tests/v2` but excludes the other historical tests.
- [x] Record every pre-existing failure without changing unrelated behavior.
- [x] Run representative isolated dry scans:
  - `python main.py --dry-run --source arbeitnow`
  - `python main.py --dry-run --source remotive`
  - `python main.py --dry-run --source himalayas`
  - `python main.py --dry-run --source linkedin`
  - `python main.py --dry-run --explain`
- [x] Run an isolated ephemeral service with temporary database/log mounts, no real `.env`, notifications and Zoho disabled, a 512 MB limit, and the health port bound to `127.0.0.1` only.
- [x] Record startup scan duration and baseline container memory with `docker stats --no-stream`.
- [x] Record baseline `/health` output.
- [x] Remove the ephemeral container and temporary runtime files after measurement.
- [x] Document whether `README.md` and `.github/workflows/deploy.yml` disagree about which tests form the CI gate.

## Definition of done

- Baseline commands and results are recorded in the progress log.
- No product behavior has changed.
- Pre-existing test failures, if any, are clearly separated from new failures.

---

# Phase 1A — Core source and funnel observability

## Objective

Add the core contracts, accounting, persistence, and presentation needed to explain where jobs disappear without changing source-specific parsing, eligibility decisions, scoring, or notification routing.

Detailed multi-board and multi-request partial-failure instrumentation is explicitly deferred to Phase 1B. In Phase 1A, existing adapters may report a complete-source outcome through the compatibility layer.

## Contracts and metric semantics

Add stable enums and typed result objects in `models/scan.py`:

- `SourceStatus`: `healthy`, `zero_results`, `partial_success`, `rate_limited`, `blocked`, `parse_error`, `network_error`, `unknown_error`
- `RejectionCode`: `duplicate_in_memory`, `company_blocklist`, `location`, `role`, `stack`, `language`, `seniority`, `salary`, `recency`, `minimum_score`, `company_cap`
- `SourceFetchOutcome`, `FilterRejection`, filter-run summary, and per-source scan summary dataclasses

Every raw job must terminate in exactly one result: accepted or one primary `RejectionCode`. Count pipeline stages as follows:

- `raw`: jobs returned by a source
- `accepted`: jobs remaining after all filters and the company cap, before database deduplication
- `unseen`: accepted jobs remaining after URL/content database deduplication
- `saved`: rows actually inserted into SQLite
- routing: saved jobs assigned to the existing `immediate`, `digest`, or `none` tier

Timestamp semantics apply to Phase 1A and Phase 1B:

- `last_completed_at` advances after every completed source attempt, regardless of outcome.
- `last_usable_at` advances for `healthy`, `zero_results`, and `partial_success`.
- `last_fully_successful_at` advances only for `healthy` and `zero_results`.
- Normal operational health displays use `last_usable_at`; `last_fully_successful_at` remains available for diagnostics.

## Expected files and modules

- New: `models/scan.py`
- New: `filters/pipeline.py`
- Update: `sources/base.py`, `main.py`, `storage/database.py`, `health.py`
- New focused tests: `tests/v2/test_observability.py`
- Progress only: `IMPLEMENTATION_PLAN.md`

Notifier modules and provider-specific adapters are not expected to change in Phase 1A. The daily status renderer currently lives in `main.py`.

## Tasks

- [x] Add the typed scan contracts and stable enum values above without adding a production dependency.
- [x] Add a `BaseSource` outcome method that categorizes complete-source results and exceptions while preserving the existing list-returning `fetch()` and `safe_fetch()` compatibility paths.
- [x] Adapt `run_scan()` to consume typed outcomes for real sources and synthesize outcomes for legacy/mock sources that only provide `safe_fetch()`.
- [x] Extract filter orchestration to `filters/pipeline.py`; keep compatibility wrappers in `main.py` for existing callers and tests.
- [x] Emit exactly one primary rejection result per raw job while preserving filter order, human-readable `--verbose`/`--explain` output, eligibility behavior, scoring, and the company cap.
- [x] Aggregate raw, rejection, accepted, unseen, saved, and routing counts overall and per source without retaining rejected `Job` objects unless verbose output is enabled.
- [x] Change `save_jobs()` to return the jobs actually inserted; callers that ignore the return value remain compatible, and scan accounting/notifications use only inserted rows.
- [x] Add an idempotent `source_scan_runs` table keyed by a shared scan identifier and source, with timestamps, duration, status, stage counts, JSON rejection/routing counts, component-error count, and a sanitized bounded error message.
- [x] Add indexes for latest-run/source lookups and delete history older than 30 days after successful metric persistence.
- [x] Keep dry-run behavior read-only: no job rows, scan-history rows, or notification state may be written.
- [x] Add storage queries for the latest overall scan, latest source attempts, and all three timestamp semantics.
- [x] Restore the latest persisted health summary during scheduler startup before the first new scan completes.
- [x] Expand `/health` additively while preserving existing keys used by deployment: `last_scan_summary.raw`, `eligible_role_matches`, `rejected`, `immediate`, `digest`, and `diagnostic`.
- [x] Add compact `unseen`, `saved`, rejection totals, and per-source operational health using `last_usable_at`; retain diagnostic timestamps without exposing secrets.
- [x] Extend `--stats` with a bounded latest-source health table.
- [x] Extend the daily Discord status with aggregate funnel counts, top rejection reasons, and at most five failed/partial source names.
- [x] Sanitize persisted/displayed exception text by removing URL query/fragment data, redacting sensitive key/value patterns, collapsing control characters, and truncating to 300 characters.
- [x] Add focused tests for contracts, complete-source status classification, compatibility, every rejection code, per-source accounting, insert counts, migration idempotency, cleanup, dry-run immutability, health restoration/compatibility, stats output, and bounded daily status output.

## Acceptance criteria

- A complete-source failure is distinguishable from a successful source returning zero jobs, while one failed source cannot abort the scan.
- Overall and per-source `raw == accepted + sum(primary rejection counts)` before database deduplication.
- Every raw job contributes to exactly one accepted/rejected terminal result.
- `unseen`, actual `saved`, and routing counts match their documented stages.
- `source_scan_runs` is created idempotently on a pre-existing database and retains no rows older than 30 days after cleanup.
- `last_completed_at`, `last_usable_at`, and `last_fully_successful_at` advance according to the documented status sets.
- `/health` remains backward compatible, restores the latest persisted summary after restart, and uses `last_usable_at` for normal source-health display.
- `--stats` and the daily status remain compact; stored/displayed errors contain no credentials, email bodies, query secrets, or unbounded text.
- Existing Germany/Berlin eligibility, match scoring, CLI behavior, deduplication, and notification thresholds/channels remain unchanged.
- Phase 1A does not claim accurate partial component reporting for existing multi-board/multi-request adapters; that remains Phase 1B.

## Verification

Run in this order:

```bash
python -m pytest tests/v2/test_observability.py -q
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase1a .
python main.py --dry-run --source arbeitnow
python main.py --dry-run --explain
```

- The explicit historical suite remains a non-blocking diagnostic: compare against the Phase 0 baseline of 911 passed and 104 failures, and introduce no additional failures.
- Run the image as an isolated ephemeral service using temporary database/log mounts, no real `.env`, notifications and Zoho disabled, a 512 MB limit, and a loopback-only health port.
- Validate startup restoration, the completed startup scan, backward-compatible `/health`, and `docker stats --no-stream`.

## Memory criteria

- Peak observed container memory must remain below 430 MiB under the same startup-scan measurement method used in Phase 0 (baseline: 319.7 MiB).
- Non-verbose scans must aggregate rejection counts without retaining rejected jobs or response bodies.
- Persist at most one row per attempted source per scan, retain 30 days, and keep stored error text at or below 300 characters.

## Definition of done

- All Phase 1A tasks and acceptance criteria are verified and recorded.
- Focused and blocking v2 tests pass; historical compatibility has no new failures.
- Docker build, representative dry scans, isolated `/health`, and memory verification pass.
- Only Phase 1A checklist/progress entries are marked complete; Phase 1B remains not started.
- Commit the phase as `feat: add core source scan observability` and stop for review.

---

# Phase 1B — Multi-board and multi-request partial-success reporting

## Objective

Make `partial_success` accurate for adapters that can return usable jobs while one or more employer boards, pages, or queries fail, without turning routine malformed individual listings into source-health incidents.

Phase 1B builds on the contracts, persistence, timestamp semantics, and presentation implemented in Phase 1A. It must not change filters, scoring, routing, schedules, or concurrency settings.

## Expected files and modules

- Shared instrumentation: `sources/base.py`, and `models/scan.py` only if the approved Phase 1A contract needs a compatible extension
- Multi-board adapters: `sources/greenhouse.py`, `sources/ashby.py`, `sources/personio.py`, `sources/lever.py`, `sources/workable.py`, `sources/jsonld.py`
- Default multi-page/query adapters where applicable: `sources/stepstone.py`, `sources/remotive.py`, `sources/himalayas.py`, `sources/idealist.py`, `sources/linkedin.py`
- New focused tests: `tests/v2/test_partial_source_outcomes.py`
- Progress only: `IMPLEMENTATION_PLAN.md`

Before editing, audit all source adapters for `_map_bounded`, `asyncio.gather(..., return_exceptions=True)`, and page/query loops. Add any additional adapter with the same component-failure behavior to this phase; do not instrument unrelated single-request adapters speculatively.

## Tasks

- [x] Add a bounded per-attempt component-issue collector to `BaseSource` using the Phase 1A status/error contracts.
- [x] Record the total component-failure count while retaining no more than five sanitized issue summaries in memory and persisting only bounded diagnostic text.
- [x] Instrument each relevant multi-board, multi-page, and multi-query adapter so board/page/query exceptions are recorded without discarding successful jobs from other components.
- [x] Classify outcomes consistently:
  - jobs and no component failures → `healthy`
  - no jobs and no component failures → `zero_results`
  - one or more successful components plus one or more component failures → `partial_success`, even when the successful components returned no jobs
  - no successfully completed component → the dominant concrete failure status (`rate_limited`, `blocked`, `parse_error`, `network_error`, or `unknown_error`)
- [x] Keep `last_usable_at` advancing for `partial_success`; do not advance `last_fully_successful_at` for partial runs.
- [x] Treat a component as a complete board, page, or query fetch/parse unit. Continue logging/skipping an isolated malformed listing without marking the entire source partial.
- [x] Preserve source failure isolation and all usable jobs when sibling components fail.
- [x] Do not add requests, retries, concurrency, retained response bodies, dependencies, or production browser support.
- [x] Add adapter-specific mocked tests for all-success, mixed success/failure, all-failed, rate-limited/blocked, and routine malformed-listing cases.
- [x] Add integration tests proving a partial source persists/displayed status and timestamp semantics correctly alongside healthy and failed sources.

## Acceptance criteria

- A multi-component source returning usable jobs plus at least one failed component is persisted and displayed as `partial_success` with correct counts.
- Usable jobs from successful components continue through filtering, deduplication, saving, and existing notification routing.
- A fully successful source and a legitimate zero-result source are not mislabeled partial.
- A malformed individual listing that is safely skipped does not by itself produce `partial_success`; failure of a complete board/page/query unit does.
- Component issue storage is bounded, sanitized, and contains no response bodies, credentials, OAuth data, webhook URLs, or email content.
- `last_completed_at`, `last_usable_at`, and `last_fully_successful_at` retain the Phase 1A semantics for partial and failed runs.
- Source-level and per-component concurrency remain at their existing configured bounds.
- Existing filtering, notification behavior, CLI output, SQLite compatibility, and Phase 1A health fields remain backward compatible.

## Verification

Run in this order:

```bash
python -m pytest tests/v2/test_partial_source_outcomes.py -q
python -m pytest tests/v2/test_observability.py -q
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase1b .
python main.py --dry-run --source greenhouse
python main.py --dry-run --source personio
python main.py --dry-run --source remotive
python main.py --dry-run --source himalayas
python main.py --dry-run --explain
```

- The explicit historical suite remains non-blocking and must add no failures to the Phase 0 baseline.
- Use mocked HTTP for automated tests. Live dry scans validate current behavior but do not determine unit-test success.
- Run an isolated 512 MB service and confirm `/health` reports partial/failed sources without losing successful source results.

## Memory criteria

- Peak observed container memory must remain below 430 MiB using the Phase 0 measurement method.
- Keep at most five component issue summaries per source attempt and each persisted error message at or below 300 characters.
- Do not increase `MAX_CONCURRENT_SOURCES`, per-board concurrency, page sizes, or the number of simultaneous source groups.

## Definition of done

- All identified multi-board and relevant multi-request adapters report component failures through the shared bounded mechanism.
- Adapter-specific and integration tests prove accurate `partial_success` behavior and non-partial malformed-listing behavior.
- Focused and blocking v2 tests pass; historical compatibility has no new failures.
- Docker build, representative live dry scans, isolated health validation, and memory verification pass.
- Commit the phase as `feat: report partial success from multi-board sources` and stop for review.

---

# Phase 2 — Employment type and contract metadata

## Objective

Support more work arrangements without weakening role or location relevance.

## Model changes

Add normalized fields to `Job`:

```python
employment_type: Literal[
    "full_time",
    "part_time",
    "fixed_term",
    "contract_employee",
    "freelance",
    "working_student",
    "internship",
    "unknown",
] = "unknown"

weekly_hours: int | None = None
contract_duration: str | None = None
freelance_rate: str | None = None
employment_type_reasons: list[str] = Field(default_factory=list)
```

Do not use `is_part_time` and `is_freelance` booleans in parallel with the enum.

## Classification policy

Use structured source fields first. Use title/tags/description heuristics only when structured data is absent.

Examples:

- `Teilzeit`, `part-time`, `20 hours/week`, `32h` → `part_time`
- `Vollzeit`, `full-time` → `full_time`
- `befristet`, `fixed term`, `12-month contract` → `fixed_term`
- Arbeitnehmerüberlassung or an employment contract through an agency → `contract_employee`
- `Freelance`, `Freiberuflich`, `B2B`, `contractor`, day rate → `freelance`
- `Werkstudent` → `working_student`
- `Internship`, `Praktikum` → `internship`

Avoid classifying every English use of “contract” as freelance.

## Profile changes

Add to `profile.toml`:

```toml
[employment]
accepted = ["full_time", "part_time", "fixed_term", "contract_employee", "freelance"]
reject = ["working_student", "internship"]
freelance_permission_required = true
preferred_weekly_hours_min = 15
preferred_weekly_hours_max = 40
```

The values above are configuration, not hardcoded logic.

## Tasks

- [ ] Add model fields and validators.
- [ ] Add a shared employment classifier.
- [ ] Update native source adapters to pass structured employment data when available.
- [ ] Add backward-compatible database columns and JSON serialization.
- [ ] Include employment type and hours in CLI output and notifications.
- [ ] Add a clear marker for `freelance_permission_required`.
- [ ] Do not reject freelance solely because permission may be required; route it separately.
- [ ] Continue rejecting working-student and internship roles through profile configuration.
- [ ] Add tests for English and German terminology, ambiguous “contract”, hours parsing, migrations, and notifier formatting.

## Definition of done

- Part-time, fixed-term, contract-employment, and freelance roles can be identified.
- The distinction between employment contract and self-employed freelance is visible.
- Existing database files migrate automatically.
- No source adapter is required to provide every new field.
- Full tests pass.

---

# Phase 3 — Requirement-aware language handling

## Objective

Stop losing relevant German-market jobs simply because their descriptions are written in German.

## Policy

The language filter should evaluate job requirements, not only the prose language.

Reject when the posting explicitly requires German above the configured maximum, including:

- B2, C1, or C2
- fluent
- native
- business fluent
- professional proficiency
- verhandlungssicher
- Muttersprache

Accept when:

- the posting is English and contains no excessive German requirement
- the posting explicitly accepts A1, A2, or B1
- the posting says German is optional, a plus, beneficial, or not required
- the posting is German but contains no explicit German-level requirement

For a German posting with no explicit language requirement:

- accept it
- attach a reason such as `posting_language_german_requirement_unspecified`
- treat it as a mild risk signal for scoring/routing, not a hard rejection

## Model/output changes

Add only fields that provide lasting value, for example:

```python
posting_language: Literal["en", "de", "other", "unknown"] = "unknown"
language_requirement_status: Literal[
    "compatible",
    "incompatible",
    "optional",
    "unspecified",
    "unknown",
] = "unknown"
language_reasons: list[str] = Field(default_factory=list)
```

## Tasks

- [ ] Replace the current English-only decision with requirement-aware evaluation.
- [ ] Use `candidate.max_german_level` from `profile.toml`.
- [ ] Keep deterministic regex/rule behavior for explicit language requirements.
- [ ] Use `langdetect` only to enrich posting language, not as the sole rejection rule.
- [ ] Show language status in explain output.
- [ ] Add comprehensive tests using German and English examples.
- [ ] Ensure explicit B2/C1/native requirements still reject correctly.

## Definition of done

- A German-language posting without an explicit high-level requirement is no longer automatically rejected.
- Explicit German requirements above B1 are rejected.
- Every language decision has an explanation.
- Full tests pass.

---

# Phase 4 — Notification routing and recall

## Objective

Surface more eligible jobs without flooding immediate notifications.

## Proposed configurable tiers

```toml
[notifications]
immediate_score = 70
digest_score = 30
explore_score = 15
daily_explore_enabled = true
```

Normalized tiers:

- `immediate`: strong matches
- `digest`: plausible matches
- `explore`: eligible but weak/uncertain matches
- `none`: diagnostics only

Freelance roles with `freelance_permission_required = true` should have a visible flag and may use a dedicated digest section rather than immediate alerts.

## Tasks

- [ ] Add `explore` to the tier model and DB migration.
- [ ] Make the per-company cap configurable; default to 5.
- [ ] Apply company cap independently enough that one category does not hide all part-time/freelance roles.
- [ ] Add a daily explore digest with a strict maximum item count.
- [ ] Keep immediate notifications concise.
- [ ] Make digest queries idempotent and mark only actually delivered jobs as notified for the relevant tier.
- [ ] Add sections by employment type.
- [ ] Add tests for thresholds, caps, digest idempotency, and freelance flags.

## Definition of done

- Eligible lower-scoring jobs are visible in a controlled daily digest.
- Immediate alerts remain high precision.
- Notification retries do not produce uncontrolled duplicates.
- Full tests pass.

---

# Phase 5 — Source groups, schedules, and 512 MB operation

## Objective

Use appropriate scan intervals and enable existing useful lightweight sources without raising peak memory risk.

## Proposed source groups

### Group A — direct employer boards

Run every 60 minutes:

- greenhouse
- ashby
- personio
- lever
- workable
- jsonld

### Group B — Germany/remote aggregators

Run every 120 minutes:

- arbeitnow
- stepstone
- remotive
- himalayas
- remoteok
- idealist

LinkedIn should be audited. Prefer email alerts over fragile scraping.

### Group C — impact/NGO sources

Run every 360 minutes:

- goodjobs
- reliefweb
- devex
- eurobrussels
- hours80k
- techjobsforgood only if it works without Playwright in production

Do not enable a source merely because an adapter exists. It must pass validation and return useful structured eligibility data.

## Runtime settings

Production target:

```env
MAX_CONCURRENT_SOURCES=1
DISABLE_PLAYWRIGHT=true
```

## Tasks

- [ ] Introduce a simple source-group schedule configuration.
- [ ] Avoid multiple overlapping scans with `max_instances=1` and coalescing.
- [ ] Add a process-level scan lock if scheduler/manual commands can overlap.
- [ ] Enable GoodJobs, ReliefWeb, and Devex after source-specific tests and live validation.
- [ ] Audit the LinkedIn source and remove it from scheduled scans if it is blocked or unreliable.
- [ ] Preserve manual `--source NAME` execution for all adapters.
- [ ] Measure peak memory during startup and representative group scans.
- [ ] Target peak container memory below 430 MiB to preserve headroom.
- [ ] Keep production free of Playwright/Chromium execution.
- [ ] Document source cadence and memory settings.

## Definition of done

- Source groups run on separate schedules.
- Scans cannot overlap and exhaust memory.
- At least the selected lightweight impact sources are active and validated.
- Representative production scans stay below the agreed memory target.
- Full tests and deployment health gate pass.

---

# Phase 6 — Job-alert email ingestion

## Objective

Increase coverage from platforms that are difficult or inappropriate to scrape by parsing job-alert emails through the existing read-only Zoho integration.

## Initial providers

Implement deterministic parsers in this order:

1. LinkedIn job alerts
2. Indeed job alerts
3. StepStone alerts
4. JOIN alerts
5. BerlinStartupJobs newsletters
6. freelancermap project alerts
7. GULP project alerts

## Design constraints

- Do not fetch attachments.
- Do not execute scripts from emails.
- Sanitize and normalize links.
- Prefer direct employer/ATS links when present.
- Deduplicate by provider item ID, normalized URL, and content hash.
- Preserve provider name as the source.
- Keep application-history extraction separate from job-alert extraction.
- Reuse the normal eligibility, role, stack, language, employment, scoring, and notification pipeline.
- Avoid circular imports between `main.py` and Zoho modules.
- Extract shared pipeline logic into a focused service module if necessary.
- Do not build a generic email framework beyond the requirements of the initial providers.

## Suggested normalized object

A parser may first produce a small `JobAlertItem`, then normalize it to `Job`.

Required parser output:

- title
- company
- location
- URL
- provider
- optional summary
- optional employment type
- optional posted time
- parsing confidence/evidence

## Tasks

- [ ] Detect job-alert emails separately from application/recruiter emails.
- [ ] Add provider-specific parsers with fixture-based tests.
- [ ] Normalize alert items into the existing job pipeline.
- [ ] Store a bounded record of processed alert items.
- [ ] Add dry-run output showing parsed items without writes or notifications.
- [ ] Add per-provider parser health metrics.
- [ ] Ensure the normal Zoho checkpoint advances only after all configured mail processing succeeds.
- [ ] Add documentation for creating useful alerts.

## Definition of done

- At least LinkedIn and Indeed alert emails produce normalized jobs.
- Job alert items pass through the same filters and deduplication as API jobs.
- Existing application tracking continues to work.
- Dry-run mode is safe and informative.
- Full tests pass.

---

# Phase 7 — Add new lightweight platforms

## Objective

Add only platforms proven to provide unique, relevant roles after the earlier recall improvements.

## Candidate order

1. WeAreDevelopers
2. GermanTechJobs
3. BerlinStartupJobs
4. Impactpool
5. Wellfound, preferably through alerts
6. freelancermap, preferably through alerts before direct scraping

## Source admission checklist

A new source is not enabled by default until it:

- [ ] has a stable public API, RSS, JSON-LD, or lightweight HTML path
- [ ] requires no authentication or prohibited automation
- [ ] returns title, company, location, URL, and enough eligibility evidence
- [ ] has tests using saved fixtures/mocked responses
- [ ] has rate-limit and failure handling
- [ ] has source-health reporting
- [ ] adds meaningful unique jobs in a seven-day comparison
- [ ] stays within memory and scan-duration budgets

## Definition of done

- Each enabled source demonstrates unique useful coverage.
- No new browser runtime is required.
- Platform terms and access constraints are respected.

---

# Phase 8 — Production rollout and evaluation

## Objective

Deploy safely and determine whether recall actually improved.

## Tasks

- [ ] Back up SQLite.
- [ ] Deploy one completed phase at a time.
- [ ] Confirm health endpoint and startup scan.
- [ ] Track seven-day metrics before and after:
  - raw jobs
  - eligible jobs
  - accepted roles
  - immediate/digest/explore jobs
  - unique companies
  - part-time/fixed-term/freelance counts
  - German-language postings retained
  - source errors
  - peak memory
- [ ] Review false positives and false negatives.
- [ ] Tune thresholds only from observed results.
- [ ] Update README and CHANGELOG.
- [ ] Remove temporary debug output and stale compatibility code.

## Definition of done

- Production remains stable for seven days.
- Peak memory remains below the agreed target.
- The number of useful unique jobs increases measurably.
- Source failures and rejection causes are visible.
- Documentation reflects production behavior.

---

# Testing strategy

## Unit tests

Cover:

- employment classification
- German/English language requirements
- filter reason codes
- match and routing thresholds
- source health classification
- alert-email parsers
- database migrations
- deduplication
- source grouping and scan locking

## Integration tests

Cover:

- source result → filter funnel → SQLite → notification tier
- German part-time role
- Berlin hybrid role
- Germany-remote freelance role
- restricted non-Germany remote role
- failed source alongside successful sources
- Zoho application email and job-alert email in the same sync

## Production-like verification

- Docker build
- health check
- startup scan
- representative manual source scans
- no overlapping jobs
- no Playwright process
- memory capture with `docker stats --no-stream`
- logs contain no secrets or email bodies

---

# Required final report for every phase

Codex must report:

1. Phase implemented
2. Files changed
3. Design decisions
4. Database migrations
5. Tests added/updated
6. Commands run and outcomes
7. Memory/runtime observations
8. Known limitations
9. Plan checklist updates
10. Exact next recommended phase

---

# Progress log

## Baseline

- Date: 2026-08-06
- Branch/worktree: `v-2` in `/home/saqib/projects/job-tracker/job-bot`
- Commit: baseline started from `3de1db0d20b909f405af51405a1cdd90fcb5d2b4`
- Existing test result:
  - Blocking: `python -m pytest tests/v2 -q` — 34 passed in 0.59s.
  - Blocking/CI-equivalent: `python -m pytest -q --timeout=30` — 34 passed in 0.60s; `pyproject.toml` makes this the same `tests/v2` gate.
  - Non-blocking historical diagnostic: `python -m pytest tests -q --timeout=30` with the complete host `tests/` directory mounted read-only — 911 passed, 104 failed, 8 warnings in 9.40s.
  - A compact `--tb=no` repeat captured the exact same 104 failing node IDs — 911 passed, 104 failed, 10 warnings in 7.84s.
- Docker build:
  - Tag: `job-bot:phase0-baseline-3de1db0`
  - Image: `sha256:b78ea9875a5f96df2836241821181b2260c642a442fcb18e0758c3bcc69a2ce0`
  - Size: 373,050,932 bytes (`docker image ls`: 373 MB)
  - Authoritative runtime: Python 3.11.15
- Peak memory: best observed startup-scan sample was 319.7 MiB / 512 MiB (62.44%); below the 430 MiB target. Nine `docker stats --no-stream` samples ranged from 255.4 to 319.7 MiB.
- Health output summary: `status=ok`, uptime 52s, startup scan completed at `2026-08-06T19:29:04.517623+00:00`, 39 jobs tracked, 11,583 raw, 39 eligible, 11,544 rejected, 8 immediate, 31 digest, 0 diagnostic. Startup scan duration was approximately 50.0s from scheduled-scan start to health completion.
- Captured `/health` payload:

  ```json
  {
    "status": "ok",
    "uptime_seconds": 52,
    "last_scan": "2026-08-06T19:29:04.517623+00:00",
    "jobs_tracked": 39,
    "next_scan_in_seconds": 2700,
    "last_scan_summary": {
      "raw": 11583,
      "eligible_role_matches": 39,
      "rejected": 11544,
      "immediate": 8,
      "digest": 31,
      "diagnostic": 0,
      "sources": {
        "greenhouse": 6186,
        "ashby": 3109,
        "personio": 1399,
        "lever": 475,
        "workable": 9,
        "jsonld": 0,
        "arbeitnow": 175,
        "stepstone": 0,
        "remotive": 34,
        "himalayas": 21,
        "remoteok": 100,
        "idealist": 35,
        "linkedin": 40
      }
    }
  }
  ```

- Notes:
  - Isolation: direct `docker run` was used without Compose or the real `.env`; database and logs used a newly created `/tmp` tree; Discord, Telegram, daily/weekly status, and Zoho were explicitly disabled; memory was limited to 512 MB; host binding was `127.0.0.1:18080`; the container and temporary tree were removed after measurement.
  - Source dry scans: Arbeitnow 175 raw / 2 accepted in 3s; Remotive 34 / 0 in 3s; Himalayas 21 / 0 in 4s; LinkedIn 39 / 12 in 3s. All commands exited 0.
  - All-default `--explain`: 11,584 raw / 39 accepted in 63s; routing was 8 immediate and 31 digest. Accepted sources were Greenhouse 16, LinkedIn 12, Personio 5, Ashby 4, and Arbeitnow 2.
  - All-default raw counts: Greenhouse 6,186; Ashby 3,109; Personio 1,399; Lever 475; Arbeitnow 175; RemoteOK 100; LinkedIn 41; Idealist 35; Remotive 34; Himalayas 21; Workable 9; JSON-LD 0; Stepstone 0.
  - Explain rejection counts: eligibility 10,203; role 1,191; recency 95; language 30; company cap 16; in-memory content hash duplicate 6; stack 4.
  - Pre-existing live-source issues: all five Stepstone queries returned HTTP 404; Personio boards for `researchgate`, `beroe-inc`, `sunhat`, `velio`, `getsafe`, and `xayn` returned 404, while `pitch` redirected to Personio; RemoteOK retried once and recovered. Source failures remained isolated.
  - CI/documentation: `.github/workflows/deploy.yml` runs `python -m pytest -v --tb=short --timeout=30`, which selects only `tests/v2` through `pyproject.toml`. The README opening states this accurately, but its later testing/CI section still claims the entire historical suite is the gate.
  - Installed direct dependency versions: httpx 0.28.1; feedparser 6.0.14; beautifulsoup4 4.15.0; APScheduler 3.11.3; aiosqlite 0.22.1; langdetect 1.0.9; discord-webhook 1.4.1; discord.py 2.7.1; python-telegram-bot 22.8; python-dotenv 1.2.2; pydantic 2.13.4; loguru 0.7.3; aiohttp 3.14.3; pytest 9.1.1; pytest-asyncio 1.4.0; pytest-timeout 2.4.0. `tomli` was not installed because Python is 3.11.

  <details>
  <summary>Complete Docker `pip freeze`</summary>

  ```text
  aiohappyeyeballs==2.7.1
  aiohttp==3.14.3
  aiosignal==1.4.0
  aiosqlite==0.22.1
  annotated-types==0.8.0
  anyio==4.14.2
  APScheduler==3.11.3
  attrs==26.1.0
  beautifulsoup4==4.15.0
  certifi==2026.7.22
  charset-normalizer==3.4.9
  discord-webhook==1.4.1
  discord.py==2.7.1
  feedparser==6.0.14
  feedparser-sgmllib==2.1.0
  frozenlist==1.8.0
  h11==0.16.0
  httpcore==1.0.9
  httpx==0.28.1
  idna==3.18
  iniconfig==2.3.0
  langdetect==1.0.9
  loguru==0.7.3
  multidict==6.7.1
  packaging==26.3
  pluggy==1.6.0
  propcache==0.5.2
  pydantic==2.13.4
  pydantic_core==2.46.4
  Pygments==2.20.0
  pytest==9.1.1
  pytest-asyncio==1.4.0
  pytest-timeout==2.4.0
  python-dotenv==1.2.2
  python-telegram-bot==22.8
  requests==2.34.2
  six==1.17.0
  soupsieve==2.9.1
  typing-inspection==0.4.2
  typing_extensions==4.16.0
  tzlocal==5.4.4
  urllib3==2.7.0
  yarl==1.24.5
  ```

  </details>

### Historical compatibility failures (non-blocking)

The following 104 failing node IDs are the recorded pre-existing historical-test baseline:

- `tests/test_database.py::TestDigestNotification::test_recent_unnotified_returns_new_jobs`
- `tests/test_database.py::TestDigestNotification::test_mark_notified_excludes_from_digest`
- `tests/test_database.py::TestDigestNotification::test_digest_does_not_repeat_after_mark`
- `tests/test_database.py::TestDigestNotification::test_recent_unnotified_respects_limit`
- `tests/test_filters.py::TestLocationFilter::test_reject_berlin_onsite`
- `tests/test_filters.py::TestLocationFilter::test_accept_berlin_remote`
- `tests/test_filters.py::TestLocationFilter::test_accept_berlin_home_office`
- `tests/test_filters.py::TestLocationFilter::test_reject_berlin_in_office`
- `tests/test_filters.py::TestLocationFilter::test_reject_tampa_fl`
- `tests/test_filters.py::TestLocationFilter::test_accept_pre_classified_worldwide`
- `tests/test_filters.py::TestLocationFilter::test_accept_pre_classified_eu`
- `tests/test_filters.py::TestLocationFilter::test_accept_pre_classified_germany`
- `tests/test_filters.py::TestLocationFilter::test_arbeitnow_worldwide_no_corroboration_defaults_germany`
- `tests/test_filters.py::TestLocationFilter::test_arbeitnow_worldwide_with_description_corroboration`
- `tests/test_filters.py::TestRemoteScopeClassification::test_germany_beats_worldwide_in_description`
- `tests/test_filters.py::TestRemoteScopeClassification::test_spain_scope`
- `tests/test_filters.py::TestRemoteScopeClassification::test_portugal_scope`
- `tests/test_filters.py::TestRemoteScopeClassification::test_dach_scope`
- `tests/test_filters.py::TestRemoteScopeClassification::test_residency_eu_scope`
- `tests/test_filters.py::TestRoleFilter::test_accept_software_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_python_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_backend`
- `tests/test_filters.py::TestRoleFilter::test_accept_internal_tools_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_react_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_nextjs_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_vue_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_django_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_fastapi_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_docker_in_description`
- `tests/test_filters.py::TestRoleFilter::test_accept_llm_ai_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_seo_engineer_not_rejected`
- `tests/test_filters.py::TestRoleFilter::test_accept_laravel_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_api_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_api_developer`
- `tests/test_filters.py::TestRoleFilter::test_accept_integration_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_technical_lead`
- `tests/test_filters.py::TestRoleFilter::test_accept_staff_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_principal_engineer`
- `tests/test_filters.py::TestRoleFilter::test_accept_application_developer`
- `tests/test_filters.py::TestMatchScore::test_partial_match`
- `tests/test_filters.py::TestMatchScore::test_no_match`
- `tests/test_main_fixes.py::TestArbeitnowGermanyDefault::test_arbeitnow_unknown_scope_defaults_germany`
- `tests/test_main_fixes.py::TestPerCompanyCap::test_max_two_per_company`
- `tests/test_main_fixes.py::TestPerCompanyCap::test_different_companies_not_capped`
- `tests/test_main_fixes.py::TestPerCompanyCap::test_cap_keeps_most_recent`
- `tests/test_main_fixes.py::TestArbeitnowOnSiteRejection::test_arbeitnow_remote_germany_accepted`
- `tests/test_main_fixes.py::TestArbeitnowOnSiteRejection::test_arbeitnow_onsite_worldwide_with_corroboration_accepted`
- `tests/test_main_fixes.py::TestPreClassifiedScope::test_idealist_worldwide_preserved`
- `tests/test_main_fixes.py::TestPreClassifiedScope::test_idealist_eu_preserved`
- `tests/test_main_fixes.py::TestPreClassifiedScope::test_non_preclassified_still_reclassified`
- `tests/test_main_fixes.py::TestRecencyFilter::test_recent_job_accepted`
- `tests/test_main_fixes.py::TestRecencyFilter::test_custom_max_age_accepts_within_range`
- `tests/test_main_fixes.py::TestRecencyFilter::test_no_posted_at_accepted`
- `tests/test_main_fixes.py::TestRecencyFilter::test_exactly_at_boundary`
- `tests/test_main_fixes.py::TestRecencyFilter::test_naive_datetime_handled`
- `tests/test_main_fixes.py::TestPerSourceMaxAge::test_reliefweb_uses_30_day_default`
- `tests/test_main_fixes.py::TestPerSourceMaxAge::test_cli_max_age_does_not_override_source`
- `tests/test_main_fixes.py::TestVerboseRejections::test_verbose_shows_recency_rejection`
- `tests/test_new_sources.py::TestSourceRegistration::test_all_sources_registered`
- `tests/test_new_sources.py::TestSourceRegistration::test_source_count_is_twenty`
- `tests/test_new_sources.py::TestSourceRegistration::test_get_sources_all`
- `tests/test_new_sources.py::TestUnknownScopeDefaults::test_hours80k_unknown_scope_defaults_worldwide`
- `tests/test_new_sources.py::TestUnknownScopeDefaults::test_idealist_unknown_scope_defaults_worldwide`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_techjobsforgood_worldwide_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_techjobsforgood_europe_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_eurobrussels_berlin_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_goodjobs_germany_remote_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_devex_worldwide_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_hours80k_worldwide_accepted`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_multiple_new_sources_in_batch`
- `tests/test_new_sources.py::TestNewSourcesFilterIntegration::test_company_cap_applies_to_new_sources`
- `tests/test_v13_features.py::TestCompanyBlocklist::test_blocklist_in_filter_pipeline`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_platform_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_internal_tools_engineer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_technical_lead`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_software_engineer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_react_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_backend_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_web_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_python_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_django_developer`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_ai_engineer_llm`
- `tests/test_v15_filters.py::TestRoleFilterV15RejectPatterns::test_accept_wordpress_support_engineer`
- `tests/test_v15_filters.py::TestMatchScoreV15::test_ngo_react_very_high_score`
- `tests/test_v15_filters.py::TestMatchScoreV15::test_python_fastapi_moderate_score`
- `tests/test_v15_filters.py::TestMatchScoreV15::test_vue_nuxt_high_score`
- `tests/test_v15_filters.py::TestMatchScoreV15::test_langchain_rag_bonus`
- `tests/test_v15_filters.py::TestMatchScoreV15::test_generic_title_no_stack_zero`
- `tests/test_v15_filters.py::TestOnsiteGermanyRejection::test_accept_onsite_germany_when_enabled`
- `tests/test_v15_filters.py::TestOnsiteGermanyRejection::test_accept_remote_germany_regardless`
- `tests/test_v15_filters.py::TestMinimumMatchScore::test_minimum_score_zero_accepts_all`
- `tests/test_v15_filters.py::TestMinimumMatchScore::test_minimum_score_accepts_high_match`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_reject_electrodynamics_engineer`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_reject_cad_engineering`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_accept_python_developer_backend`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_accept_senior_software_engineer`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_accept_backend_developer`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_accept_junior_backend_engineer`
- `tests/test_v15_filters.py::TestFilterPipelineRealJobs::test_accept_software_engineer_ios_core_product`
- `tests/test_v15_sources.py::TestSourceRegistrationV15::test_total_source_count`
- `tests/test_weekly_digest.py::TestWeeklyDigestCli::test_run_weekly_digest_cli_calls_digest`
- `tests/test_weekly_digest.py::TestBackfillMatchScores::test_returns_zero_when_no_keywords_match`
- `tests/test_weekly_digest.py::TestBackfillMatchScores::test_returns_count_of_updated`
- `tests/test_weekly_digest.py::TestBackfillCli::test_run_backfill_cli_calls_backfill`

## Phase 1A — Core source and funnel observability

- Status: Completed on 2026-08-06
- Commit: `feat: add core source scan observability` (this Phase 1A commit)
- Tests:
  - Focused: `python -m pytest tests/v2/test_observability.py -q` — 40 passed.
  - Blocking v2: `python -m pytest tests/v2 -q` — 74 passed.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 74 passed.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30 --tb=no` — 951 passed, 104 failed, 10 warnings; the failing node IDs exactly match the Phase 0 baseline, with no new failures.
- Peak memory: 322.5 MiB / 512 MiB (62.98%), 2.8 MiB above the 319.7 MiB Phase 0 baseline and 107.5 MiB below the 430 MiB target.
- Notes:
  - Final image: `job-bot:phase1a`, `sha256:14a52974e9378b1700f7738471d9b387e9649e05eefec218b1b62f11cb32b778`, 373,528,164 bytes.
  - Final isolated startup scan completed in 57.2 seconds: 11,592 raw, 39 accepted, 39 unseen, 39 actually saved, 8 immediate, 31 digest, and 0 diagnostic. Overall and all 13 per-source accounting invariants passed.
  - Live source outcomes distinguished JSON-LD's healthy zero results from StepStone's complete-source `unknown_error`; the remaining default sources returned usable results. Detailed mixed board/page/query partial-success instrumentation remains deferred to Phase 1B.
  - Dry-run proof used both an absent database and an existing sentinel database. The absent file was not created, and the existing file's SHA-256 and mtime remained unchanged after both Arbeitnow and all-source `--explain` scans.
  - `/health` retained all deployment-facing fields and added accepted/unseen/saved, rejection totals, and compact source health. A seeded restart restored the persisted summary and operational timestamps before the startup scan completed.
  - The source-health `--stats` table and bounded daily status formatting were verified; stored/displayed diagnostics are sanitized and limited to 300 characters.

## Phase 1B — Multi-board and multi-request partial-success reporting

- Status: Completed on 2026-08-06
- Commit: `feat: report partial success from multi-board sources` (this Phase 1B commit)
- Tests:
  - Phase 1A regression: `python -m pytest tests/v2/test_observability.py -q` — 40 passed in 0.73s.
  - Phase 1B focused: `python -m pytest tests/v2/test_partial_source_outcomes.py -q` — 30 passed in 0.63s.
  - Blocking v2: `python -m pytest tests/v2 -q` — 104 passed in 1.21s.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 104 passed in 1.15s.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30` — 981 passed, 104 failed, 10 warnings in 8.01s. A JUnit node-ID comparison confirmed the 104 failures exactly match the Phase 0 baseline, with no new or missing failing node IDs.
- Peak memory: 325.2 MiB / 512 MiB (63.51%), 5.5 MiB above Phase 0, 2.7 MiB above Phase 1A, 104.8 MiB below the 430 MiB target.
- Notes:
  - Added one shared per-attempt collector that counts every failed component, retains at most five sanitized issue details, strips URL queries/fragments and secrets, and applies deterministic complete-failure precedence: rate limited, blocked, network, parse, unknown.
  - Instrumented all scoped/default multi-unit adapters: Greenhouse, Ashby, Personio, Lever, Workable, JSON-LD, StepStone, Remotive, Himalayas, Idealist, and LinkedIn. Existing request/page concurrency, normalization, deduplication, filters, queries, retries, and endpoint behavior were preserved.
  - Automated adapter coverage uses mocked HTTP/component calls and saved JSON response fixtures; no test depends on a live platform.
  - Requested live dry scans: Greenhouse `healthy` / 6,190 jobs / 0 issues; Ashby `healthy` / 3,114 / 0; Personio `partial_success` / 1,399 / 1; Lever `healthy` / 475 / 0; Workable `healthy` / 9 / 0; JSON-LD `zero_results` / 0 / 0; StepStone `unknown_error` / 0 / 5.
  - The Personio `pitch` board redirects to `https://personio.com` and is the live partial failure. The configured ResearchGate, Beroe, Sunhat, Velio, Getsafe, and Xayn XML URLs return 404 but their HTML fallbacks currently succeed, so those boards remain successful components. StepStone's five unchanged query requests all return HTTP 404 and correctly aggregate to complete failure rather than partial success.
  - The all-default `--explain` dry scan completed with 11,595 raw jobs and 39 accepted. Other multi-unit default outcomes were Remotive `healthy` / 34 / 0, Himalayas `healthy` / 21 / 0, Idealist `healthy` / 36 / 0, and LinkedIn `healthy` / 42 / 0.
  - Final image: `job-bot:phase1b`, `sha256:36951a523690c14e12dd24baf33ea11433561777383b515a910bba128df7e5eb`, 373,715,457 bytes.
  - The isolated 512 MiB service used a temporary database/log tree, no `.env`, disabled notifications/Zoho, and loopback-only `127.0.0.1:18081`. Its startup scan completed in 53.9 seconds with 11,595 raw, 40 accepted/unseen/saved, 8 immediate, 32 digest, and 0 diagnostic jobs.
  - `/health`, `--stats`, and daily status expose compact source status, total issue count, bounded sanitized summary, `last_usable_at`, and diagnostic `last_fully_successful_at`; no per-board detail list is exposed. Live Personio advanced `last_usable_at` but had no fully-successful timestamp, while complete-failure StepStone advanced neither usable timestamp.
  - No database migration or dependency was needed. External board/query repair and non-default optional multi-unit adapters remain deferred; Phase 2 was not started.

## Phase 2

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 3

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 4

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 5

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 6

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 7

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

## Phase 8

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:
