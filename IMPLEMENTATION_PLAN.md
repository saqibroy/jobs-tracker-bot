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

# Phase 2A — Employment model and classification

## Objective

Represent employment relationship, work schedule, and contract term independently, then classify and persist those dimensions without weakening existing location, language, scoring, or notification behavior.

Phase 2A owns the shared model, deterministic heuristic fallback, compatibility policy, persistence, and presentation. Native source mappings are deferred to Phase 2B; source adapters may change in Phase 2A only when a compatibility adjustment is required by the new `Job` defaults.

## Domain model

Add these normalized fields to `Job`:

```python
employment_relationship: Literal[
    "employee",
    "contract_employee",
    "freelance",
    "working_student",
    "internship",
    "unknown",
] = "unknown"

work_schedule: Literal[
    "full_time",
    "part_time",
    "unknown",
] = "unknown"

contract_term: Literal[
    "permanent",
    "fixed_term",
    "unknown",
] = "unknown"

weekly_hours: int | None = None
contract_duration: str | None = None
freelance_rate: str | None = None
employment_reasons: list[str] = Field(default_factory=list)
freelance_permission_required: bool = False
```

The first three fields are independent: combinations such as part-time fixed-term employee, full-time fixed-term employee, full-time contract employee, and part-time freelance must round-trip unchanged. Do not add parallel `is_part_time`, `is_fixed_term`, `is_contract_employee`, or `is_freelance` flags. `freelance_permission_required` is a derived profile-policy marker, not a second freelance classification or a routing field; it is true only when `employment_relationship == "freelance"` and the profile setting is enabled.

Validators must accept only the documented literals, normalize missing values to safe unknown/`None` defaults, reject invalid `weekly_hours` values outside 1–168, and keep optional reason/duration/rate text bounded. Existing rows and adapters that omit every field must continue to validate as completely unknown metadata.

## Classification and precedence

Implement one shared, typed employment classifier that can accept optional normalized structured values per dimension. Run it before employment compatibility filtering. Precedence is evaluated independently for relationship, schedule, contract term, weekly hours, contract duration, and freelance rate:

1. A supported, successfully normalized structured source value is authoritative for that dimension.
2. Heuristics may fill another unknown dimension and may add non-conflicting evidence, but cannot replace an authoritative structured value.
3. Title and tags are searched before description text; all heuristic matches are deterministic and recorded with stable, bounded entries in `employment_reasons`.
4. Contradictory heuristic matches leave that dimension `unknown` unless a deterministic specific signal resolves them. Absence of evidence never implies `employee`, `permanent`, or `full_time`.

Classification rules must include:

- `Teilzeit`, `part-time`, and explicit sub-full-time weekly hours → `work_schedule="part_time"`.
- `Vollzeit` and `full-time` → `work_schedule="full_time"`.
- `unbefristet` and `permanent` → `contract_term="permanent"`.
- `befristet`, `fixed-term`, and explicit duration phrases such as `12-month contract` → `contract_term="fixed_term"`, with bounded `contract_duration` when the duration itself is explicit.
- Ordinary employee wording may set `employment_relationship="employee"` only when positive employee evidence exists.
- `Arbeitnehmerüberlassung`, temporary-agency employment, or employment through a staffing agency → `employment_relationship="contract_employee"`.
- `freelance`, `freelancer`, `freiberuflich`, `selbstständig`, `B2B`, or an explicit day/hourly freelance rate → `employment_relationship="freelance"` and bounded `freelance_rate` when present.
- `Werkstudent` or `working student` → `employment_relationship="working_student"`.
- `Praktikum` or `internship` → `employment_relationship="internship"`.
- The bare English word `contract` is insufficient to classify freelance or contract-employee relationship; surrounding explicit employment, agency, B2B, or self-employment evidence is required.

Weekly-hours parsing must be unit- and context-bound. Support `20h/week`, `20 h/week`, `20 hours per week`, `20 Stunden/Woche`, `30–32 hours/week`, `32 Std./Woche`, and compact schedule labels such as `32h`. For a range, persist its upper bound as the conservative normalized `weekly_hours` value (`30–32` becomes `32`) and retain a stable range-evidence reason. An explicit value of 1–32 weekly hours is part-time evidence; 33 or more hours remains schedule-unknown unless an explicit full-time/part-time label resolves it. Lack of hours is never full-time evidence. Regexes must require an hours/schedule context and must not capture salaries, vacation days, years of experience, dates, percentages, or a `12-month` contract duration.

## Profile policy and terminal rejection ownership

Add:

```toml
[employment]
accepted_relationships = ["employee", "contract_employee", "freelance", "unknown"]
rejected_relationships = ["working_student", "internship"]
accepted_schedules = ["full_time", "part_time", "unknown"]
freelance_permission_required = true
preferred_weekly_hours_min = 15
preferred_weekly_hours_max = 40
```

Validate configured values against the model literals. Accepted and rejected relationship sets must be disjoint; configuration loading fails clearly if they overlap. The gate rejects a relationship explicitly listed in `rejected_relationships` or a known relationship absent from `accepted_relationships`, and rejects a known schedule absent from `accepted_schedules`. `unknown` remains accepted in both default lists to preserve jobs with no employment evidence. The preferred weekly-hours keys are optional advisory bounds: omission disables that bound, values must be within 1–168, and minimum cannot exceed maximum. In Phase 2A the preferred range is exposed for explanation/presentation only and does not create a terminal rejection. Contract term is not a gate because permanent and fixed-term work are both accepted.

Add `employment_relationship` as a stable `RejectionCode` and insert one employment compatibility gate after location and before the existing role gate. It is the sole owner of terminal working-student/internship and configured relationship/schedule rejection. Remove `intern`, `internship`, `working student`, and `werkstudent` from `[roles].reject` and remove the equivalent hardcoded role/seniority checks; the employment classifier and gate must still recognize those title terms. Do not duplicate the same decision in the role gate. Jobs rejected earlier by the existing ordered gates retain that earlier single terminal result, and every raw job must continue to satisfy `raw == accepted + sum(primary rejection counts)` overall and per source.

## Tasks

- [x] Add the independent `Job` fields, typed literals/validators, and shared classifier with structured-over-heuristic inputs ready for Phase 2B.
- [x] Add the `[employment]` profile section, configuration validation, derived freelance-permission marker, and the single employment compatibility gate/rejection code described above.
- [x] Add deterministic English/German relationship, schedule, term, hours, duration, and rate heuristics without a new dependency.
- [x] Add explicit, idempotent SQLite columns for all persisted Phase 2A fields; JSON-encode `employment_reasons`; update every save/deserialization/query path that reconstructs jobs; give old rows safe unknown/`None`/false defaults.
- [x] Show known employment dimensions, weekly hours, and the freelance-permission marker in normal CLI job output and explain output, Discord job formatting, and Telegram job formatting. Keep output compact and omit unknown/empty values.
- [x] Preserve existing `none`/`digest`/`immediate` tier calculation, database notification state, channel selection, send timing, and delivery behavior. Phase 2A must not separately route or suppress freelance jobs.
- [x] Add focused model, classifier, policy, pipeline-accounting, migration, persistence, CLI, Discord, Telegram, and notification-routing regression tests.
- [x] Update only the Phase 2A checklist and progress entry after all Phase 2A verification passes; commit as `feat: classify employment relationships and schedules` and stop for review.

## Acceptance criteria

Automated tests must cover at least:

- full-time employee; part-time employee; part-time fixed-term employee; full-time fixed-term employee; and permanent part-time employee
- freelance with full-time or unspecified schedule; part-time freelance; contract employee; working student; internship; and completely unknown metadata
- English and German terminology; ambiguous bare `contract`; explicit employee evidence; and conflicting heuristic evidence
- every listed weekly-hours form; range normalization; and rejection of salary, vacation-day, experience-year, date, percentage, and contract-duration false positives
- per-dimension structured-over-heuristic precedence through the shared Phase 2B-ready API, including structured schedule plus heuristic contract term and structured/heuristic conflict
- literal and hours validation; optional/bounded strings and reasons; profile validation; and the derived freelance-permission marker
- migration from a representative Phase 1 database, idempotent repeated initialization, safe defaults on old rows, and complete save/read round trips
- one-terminal-rejection accounting when the employment gate rejects, including overall and per-source counts
- compact CLI/explain, Discord, and Telegram formatting, including the freelance-permission marker
- unchanged notification tiers and delivery selection for otherwise identical employee and freelance jobs

Existing location eligibility, role/stack evaluation apart from the transferred student/intern ownership, language behavior, scoring, company caps, deduplication, and notification behavior must remain unchanged. No native source adapter is required to populate any new field in Phase 2A.

## Verification and memory

Run focused Phase 2A tests first, then:

```bash
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase2a .
python main.py --dry-run --source arbeitnow
python main.py --dry-run --explain
```

The blocking v2 suite must pass. The historical diagnostic must introduce no failures beyond the verified 104 pre-existing failures. Run an isolated ephemeral 512 MiB service with temporary database/log mounts, notifications and Zoho disabled, and loopback-only health binding; validate `/health`, unchanged routing totals, and peak memory using the Phase 0 method. Peak observed memory must remain below 430 MiB (Phase 1B reference: 325.2 MiB). Classification must use lightweight rules/regexes and existing packages only; do not add ML/NLP models or a heavyweight dependency.

## Definition of done

- Every independent combination in scope is representable, classified deterministically, persisted, and displayed.
- Working-student/internship incompatibility has one profile-driven terminal gate and stable rejection code without duplicate role-gate ownership.
- Freelance permission is visible but does not affect routing or delivery.
- Focused and blocking tests, historical compatibility, Docker build, dry scans, `/health`, and memory verification pass and are recorded.
- Commit Phase 2A separately and stop before Phase 2B.

---

# Phase 2B — Structured source employment metadata

## Objective

Map employment metadata that source providers genuinely expose, giving supported structured values higher authority than Phase 2A text heuristics while preserving source isolation and partial-success reporting.

Prioritize enabled/default sources and direct ATS adapters: Greenhouse, Ashby, Personio, Lever, Workable, and JSON-LD first, followed by default aggregator adapters with saved evidence. Do not require every adapter to populate every dimension and do not infer a value merely because another provider exposes a similarly named field.

## Mapping and precedence policy

- Audit current provider API responses and saved fixtures before editing each adapter. In the Phase 2B progress entry, record source by source the raw field, observed values, normalized dimension/value, fixture or diagnostic evidence, and unsupported dimensions.
- Map only documented or repeatedly observed structured fields. For example, evaluate Personio `employmentType`/`schedule`, Lever `categories.commitment`, Workable `employment_type`, JSON-LD `employmentType`, and any equivalent actual fields found during the audit; their presence in current tags is not by itself proof of complete semantics.
- Normalize provider values through shared mapping helpers into the Phase 2A domain literals. Keep provider-specific raw-field interpretation inside the adapter and shared employment semantics inside the classifier; do not duplicate heuristic dictionaries across sources.
- Apply precedence independently per dimension: a supported structured value replaces a conflicting heuristic value for that dimension, while heuristics may enrich dimensions the source did not supply. Unsupported, empty, malformed, or unknown provider values fall back to Phase 2A heuristics and add no invented classification.
- Preserve bounded `employment_reasons` that identify structured versus heuristic evidence without storing full response bodies or sensitive content.
- Keep malformed individual listings non-partial and keep board/page/query failures integrated with the Phase 1B bounded component-outcome mechanism. Employment mapping failure for one listing must not discard other usable listings or crash a source.

## Tasks

- [x] Audit default/direct ATS response shapes, documentation where available, and saved/live fixtures; add the source-by-source evidence table to this phase's progress log.
- [x] Add normalized structured mappings only to adapters with verified native fields, in the priority order above.
- [x] Add or update sanitized saved fixtures and adapter tests for every mapping; mock external HTTP in automated tests.
- [x] Test structured precedence, partial structured enrichment, unknown-value fallback, malformed-value isolation, and heuristic fallback when the native field is absent.
- [x] Run focused adapter/integration tests, the complete verification sequence, representative live diagnostic dry scans for mapped default sources, isolated `/health`, and memory measurement.
- [x] Update only the Phase 2B checklist and progress entry after verification; commit as `feat: map structured employment metadata from job sources` and stop for review.

## Acceptance criteria

- The progress log documents which structured employment fields each audited source actually exposes and which dimensions remain unsupported.
- Every mapped value is backed by provider data and a saved-fixture test; no source-specific value is invented and no adapter is required to fill every field.
- Structured data wins only for its own dimension; other unknown dimensions retain deterministic heuristic fallback.
- Fixture tests cover structured/heuristic conflicts, structured enrichment plus fallback, absent/unknown native values, and malformed listing isolation.
- Existing source status and component issue counts remain correct, usable results survive sibling failures, and no regression is introduced in partial-success reporting.
- Blocking v2 tests pass, the historical diagnostic adds no failures beyond the verified 104, live diagnostics remain non-blocking, and peak memory remains below 430 MiB with no meaningful regression from the Phase 2A measurement.

## Verification and definition of done

Run mapped-adapter fixture tests first, then:

```bash
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase2b .
python main.py --dry-run --source greenhouse
python main.py --dry-run --source ashby
python main.py --dry-run --source personio
python main.py --dry-run --source lever
python main.py --dry-run --source workable
python main.py --dry-run --source jsonld
python main.py --dry-run --explain
```

Add or remove individual live diagnostic commands to match the adapters proven to contain structured mappings; do not make live platforms part of automated test success. Run the same isolated 512 MiB `/health` and memory check used for Phase 2A. Phase 2B is complete only when the audit, supported mappings, fixture tests, full verification, source-outcome regression checks, and progress entry pass. Commit Phase 2B separately and stop before Phase 3.

---

# Phase 3 — Requirement-aware language handling

## Objective

Stop rejecting relevant German-market jobs merely because their advertisement prose is German. Evaluate explicit German hiring requirements against the candidate profile while keeping advertisement-language detection as separate, non-gating enrichment.

Phase 3 owns posting-language enrichment, explicit German-requirement extraction, compatibility, bounded explanations, persistence, concise CLI/explain presentation, tests, live diagnostics, and runtime verification. It must not change match-score calculation, notification tiers or thresholds, introduce explore routing, route German postings separately, reduce scores based on advertisement language, alter company caps, or change source scheduling/concurrency. Phase 4 may later decide whether language uncertainty affects routing.

## Normalized model and configuration

Add only these lasting normalized `Job` fields:

```python
posting_language: Literal["en", "de", "other", "unknown"] = "unknown"
german_requirement_status: Literal[
    "compatible",
    "incompatible",
    "optional",
    "unspecified",
    "unknown",
] = "unknown"
german_requirement_level: Literal[
    "none",
    "a1",
    "a2",
    "b1",
    "b2",
    "c1",
    "c2",
    "fluent",
    "business_fluent",
    "native",
    "unknown",
] = "unknown"
language_reasons: list[str] = Field(default_factory=list)
```

`posting_language` describes the language of the advertisement. The German requirement fields describe German-language hiring requirements; they do not model proficiency in every language. Use `german_requirement_level`, rather than `german_required_level`, because it must also preserve optional evidence such as `german_requirement=b2:optional`.

Add one validated, typed language policy loaded from `[candidate]`. Require `max_german_level`, case-normalize it, accept only A1, A2, B1, B2, C1, or C2, and fail clearly when it is missing or invalid. Do not introduce another hardcoded B1 default. Keep `accepted_languages` backward compatible and document it as languages that can satisfy an unrestricted working-language requirement; it is not an allow-list for advertisement prose and does not establish any CEFR, fluent, business-fluent, or native proficiency.

Bound `language_reasons` to at most eight unique entries of at most 120 characters each. Persist stable evidence codes rather than sentences or posting excerpts, for example:

- `posting_language=de`
- `german_requirement=b1:required`
- `german_requirement=b2:optional`
- `german_requirement=none:not_required`
- `german_requirement=fluent:required`
- `german_requirement=business_fluent:required`
- `german_requirement=native:required`
- `alternative_language_requirement=english_explicit_level_unmodeled`

## Requirement extraction and comparison policy

Keep the existing `passes_language_filter(job) -> bool` compatibility interface for current callers, backed by one centralized evaluator that populates normalized metadata before pass/fail. Preserve the single terminal `language` rejection code and its current pipeline position after employment, role, and stack and before seniority. Every raw job must still have exactly one terminal result, and overall and per-source `raw == accepted + sum(primary rejection counts)` must remain true.

Use lightweight deterministic regex/rules over title, tags, and description; add no NLP/ML dependency. Identify `German`/`Deutsch`, explicit CEFR levels, strong descriptors, required/minimum wording, optional/preferred wording, negation, `and`/`or` alternatives, and unrelated benefit/course contexts. Inspect enough surrounding clause text to determine context, but retain only bounded stable evidence.

Classify each language clause deterministically:

1. Exclude clearly unrelated evidence such as a German B2 course being provided or B2 mentioned for another subject.
2. Apply explicit negation such as `German is not required` or `Deutschkenntnisse sind nicht erforderlich`.
3. Apply optional/preferred context such as plus, preferred, beneficial, advantageous, desired later, `nice to have`, or `von Vorteil`.
4. Apply explicit hard requirement/minimum context, including direct qualification-list forms such as `C1 German` or `Deutsch auf C1-Niveau` when they clearly state a hiring requirement.
5. Treat vague, conflicting, or genuinely unclear wording as ambiguous rather than inventing an incompatible requirement.

Negated and optional context controls the matched level: `German B2 is a plus`, `Fluent German would be nice to have`, `verhandlungssicheres Deutsch von Vorteil`, and `native German preferred` must not reject. When several statements appear, compare only clearly required German evidence; use the highest clear required CEFR level, but do not promote a higher optional or future-desired level into a requirement. Thus `German B1 required, B2 preferred` is compatible at max B1. Ambiguous evidence is accepted with `german_requirement_status="unknown"` and a bounded reason.

Centralize the comparison policy:

- CEFR: A1 < A2 < B1 < B2 < C1 < C2; a clear required level passes when it is at or below the configured maximum and rejects when above it.
- Plain `fluent German` normalizes to `fluent` and, when required, has a minimum B2 comparison threshold.
- `business fluent German`, `professional working proficiency in German`, and `verhandlungssicher Deutsch` normalize to `business_fluent` and, when required, have a minimum C1 comparison threshold.
- `native German`, German mother tongue, and `Muttersprache` normalize to `native`; a CEFR-only candidate maximum cannot satisfy a required native-language constraint.
- Optional/preferred/nice-to-have forms of every CEFR level or strong descriptor remain compatible regardless of the configured maximum.

Use these aggregate statuses:

- `compatible`: a clear mandatory German requirement is within the configured capability, or a recognized unrestricted alternative-language branch is satisfied.
- `incompatible`: a clear independently mandatory German requirement exceeds the configured capability.
- `optional`: German is optional, preferred, beneficial, or explicitly not required.
- `unspecified`: no explicit German hiring requirement was found; this is a passing decision, including for German advertisements.
- `unknown`: German is mentioned but its hiring significance is ambiguous, or a potentially satisfying alternative has an explicit proficiency constraint the current profile cannot verify.

Handle alternative-language requirements without inferring English proficiency:

- `accepted_languages = ["en"]` can satisfy an unrestricted English alternative, but not `fluent English`, `English C1`, business-fluent English, native English, or another explicit English proficiency requirement.
- `German B1 or English` is compatible because German B1 is compatible; `German B2 or English` is compatible through the unrestricted English branch.
- `German B1 or fluent English` is compatible through German B1 without making an English-proficiency assumption.
- `German B2 or fluent English` and `German B2 or English C1` pass conservatively as `unknown`: German B2 is incompatible at max B1, but the explicit English proficiency alternative is unmodeled and must be recorded as `alternative_language_requirement=english_explicit_level_unmodeled` rather than assumed or rejected.
- `German B2 and English` and `German B2 and fluent English` reject at max B1 because the incompatible German branch is independently mandatory.
- Do not add an English or general per-language proficiency model in Phase 3; a future phase may add one if justified.

## Posting-language policy

Use `langdetect` only for `posting_language`, retain `DetectorFactory.seed = 0`, and preserve the current bounded detection sample of title plus the first 300 description characters. Fewer than 20 usable characters or a detection exception maps to `unknown`; English maps to `en`, German to `de`, and another reliable result to `other`.

Never use `posting_language` as a compatibility gate. German, other-language, short, and undetectable advertisements pass unless explicit German requirement evaluation establishes incompatibility. In particular, do not replace the old English-only filter with a hidden `accepted_languages` prose-language gate.

## Persistence and presentation

Persist all four normalized fields because they support historical inspection, debugging, later Phase 4 analysis, and concise presentation. Add explicit idempotent SQLite columns with safe `unknown`/`[]` defaults for Phase 2B databases, JSON-encode bounded `language_reasons`, verify old-row reconstruction and round trips, and make no destructive migration. Do not change `source_scan_runs` semantics.

Keep normal output concise and omit ordinary English/unspecified metadata. Add at most one compact CLI line when a posting is German or contains explicit/ambiguous German evidence, for example `🗣 German posting · German requirement unspecified`, `🗣 German B1 required`, or `🗣 German B2 preferred`. Show bounded evidence in `--explain`; render an incompatible terminal explanation such as `Language: German B2 required; candidate max B1`.

Do not add language metadata to Discord, Telegram, or scheduled digest messages in Phase 3. Persisted metadata plus CLI/explain output provide sufficient visibility without notification noise. Phase 3 must not alter scoring, immediate/digest thresholds, notification tiers, or routing.

## Tests

Add focused tests for:

- Posting language: English, German, other, short text, detection failure, and deterministic detection.
- Compatible requirements: A1/A2/B1 required; optional, preferred, plus, beneficial, nice-to-have, `von Vorteil`, and explicit not-required forms; German posting with no requirement.
- Configured comparison: B2 required rejects at max B1 and accepts at max B2; case normalization; missing/invalid CEFR configuration.
- Strong descriptors: fluent rejects at B1 and accepts at B2; business fluent/professional/`verhandlungssicher` reject at B2 and accept at C1; native rejects even at C2 under the CEFR-only model.
- Optional descriptors: business-fluent preferred, `verhandlungssicheres Deutsch von Vorteil`, fluent optional, and native nice-to-have all pass regardless of max CEFR.
- Context: B2 preferred, B2 nice-to-have, negated B2, English required plus German optional, B1 required plus B2 preferred, vague German skills, German B2 course provided, and unrelated B2 text.
- Alternatives: every approved `German ... or/and English ...` case above, including the bounded unmodeled-English-level reason and no assumption of English proficiency.
- Pipeline: exactly one terminal language rejection, overall/per-source accounting invariants, employment rejection still before language, accepted German jobs proceed to scoring unchanged, and language evaluation does not alter employment metadata.
- Persistence: Phase 2B migration, repeated migration, safe defaults, normalized-field round trip, and valid/malformed JSON reason reconstruction.
- Routing regression: otherwise identical accepted English, German-unspecified, and German-B1-compatible jobs receive the same match score and notification tier unless an existing independent scoring input differs.

Use mocked detection/external HTTP in automated tests. Run focused language, pipeline, storage, presentation, employment, and routing tests first, followed by:

```bash
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase3 .
python main.py --dry-run --source arbeitnow
python main.py --dry-run --explain
```

Blocking v2 tests must pass. The historical diagnostic must add no failures beyond the verified Phase 2B result of 1,086 passed and 104 known failures.

## Live diagnostics and runtime verification

Before implementation, capture a current live sample of terminal language rejections and classify bounded counts as: merely German advertisement prose, explicit B2, explicit C1, explicit C2, fluent, business-fluent/professional, native, other detected advertisement language, and ambiguous. Output only aggregate counts plus at most three source/title identifiers per category; never print, persist, or commit descriptions.

After implementation, compare language rejection count, German postings newly accepted, explicit incompatible German jobs still rejected, accepted total, and immediate/digest/diagnostic totals. Success means accurate compatibility, not maximum acceptance.

Run the same isolated 512 MiB service and `/health` validation used for Phase 2B. Record startup duration against the Phase 2A reference of approximately 51.4 seconds, the Phase 2B reference of approximately 93.6 seconds, and a paired pre-Phase-3 run. If the paired Phase 3 increase exceeds 10%, investigate the language implementation before completion; do not optimize source scheduling or concurrency. Record peak memory against 347.1 MiB, require it below 430 MiB, and investigate a Phase 3 increase above 10 MiB. Add no heavy NLP package or model.

## Tasks

- [x] Add the normalized model, validated language policy, bounded stable reasons, and centralized requirement-aware evaluator.
- [x] Add deterministic posting-language enrichment and required/optional/negated/alternative German requirement extraction.
- [x] Integrate the evaluator at the existing language gate without changing terminal accounting or any other gate.
- [x] Add the idempotent SQLite migration, safe reconstruction, and round-trip support.
- [x] Add concise CLI/explain presentation; keep Discord, Telegram, digest, scoring, and routing unchanged.
- [x] Add the complete configuration, extraction, pipeline, persistence, employment, and routing regression matrix.
- [x] Capture the bounded pre/post live diagnostics and verify Docker, `/health`, memory, and startup duration.

## Definition of done

- A posting is never rejected merely because its prose is German or another detected language.
- German advertisements with no explicit German requirement pass with `german_requirement_status="unspecified"` and a bounded explanation.
- Explicit German requirements are compared through the configured candidate maximum and the documented fluent/business-fluent/native policies; optional and ambiguous forms avoid aggressive rejection.
- Alternative-language `or`/`and` behavior follows the documented rules without inventing English proficiency or adding a general proficiency model.
- All normalized fields migrate and round-trip safely; every language decision has bounded stable evidence and every raw job retains exactly one terminal result.
- Employment behavior, match scores, notification tiers/thresholds, routing, company caps, source scheduling, and Phase 4 remain unchanged.
- Focused and blocking tests pass, the historical diagnostic adds no failures, Docker/dry scans/health pass, memory remains below 430 MiB, and startup duration is recorded and investigated when required.
- Update only the Phase 3 checklist/progress entry after implementation verification, commit Phase 3 separately, and stop before Phase 4.

---

# Phase 4A — Notification tier and delivery-state foundation

## Objective

Make notification delivery idempotent and capable of supporting immediate, digest, explore, retries, and multiple configured destinations without duplicate or lost delivery state. Phase 4A builds the delivery foundation while preserving the current effective routing thresholds, Discord destination behavior, digest cadence, and weekly NGO digest semantics.

Phase 4A must not lower recall thresholds, enable production explore routing, tune the company cap, change employment compatibility, or begin Phase 5 scheduling work.

## Verified implementation baseline and current risks

Start Phase 4A from the verified Phase 3 reference:

- blocking v2: 297 passed
- historical diagnostic: 1,174 passed and 104 known failures
- peak memory: 352.2 MiB, below the 430 MiB target
- startup scan: approximately 91.6 seconds
- current live routing sample: 8 immediate, 50 digest, 0 diagnostic
- effective thresholds: immediate 70 and digest 45
- per-company cap: 2
- `MINIMUM_MATCH_SCORE`: 0 by default

The implementation audit found these delivery-state risks:

- `jobs.notified` is one global boolean shared by tiers and destinations.
- Immediate delivery marks every selected job globally notified after channel calls even though current notifier methods swallow some per-job failures and return no durable success set.
- A failed immediate delivery has no database-backed retry path after the job becomes seen by deduplication.
- The digest is Discord-only and queries only `notified=0` digest rows inside a short recent-hours window with a limit, so overflow can become too old for the next query without ever being delivered.
- Routing metrics currently recognize only immediate, digest, and diagnostic, so a future explore value would be dropped or misclassified.
- Weekly NGO digest intentionally ignores `notified` and repeats a current seven-day summary; it is a distinct behavior that must not be migrated into one-time receipts.

## Tier and delivery-state contracts

Extend the normalized notification tier to:

```python
Literal[
    "none",
    "explore",
    "digest",
    "immediate",
]
```

`notification_tier` answers only how a job should be routed. It must not encode whether Discord or Telegram received it, which Discord webhook was selected, whether a grouped digest succeeded, or which destination still needs a retry.

Keep Phase 4A routing equivalent to the current policy:

- `immediate`: score at least 70
- `digest`: score from 45 through 69
- `none`: score below 45
- `explore`: valid in the model and persistence layer, but assigned to no production job until Phase 4B enables the policy

The existing `notification_tier` SQLite column is unconstrained text and does not require a destructive column migration. The Phase 4A schema migration is the new receipt table described below.

## Logical delivery destinations

Preserve the current immediate Discord behavior exactly:

- A general job selects the general Discord webhook.
- An NGO job selects the NGO Discord webhook when it is configured.
- An NGO job falls back to the general Discord webhook when the NGO webhook is not configured.
- A job is never intentionally sent to both Discord webhooks for the same immediate delivery.
- Telegram is an independent destination when its token and chat are configured.

Use stable logical receipt destination identifiers:

- `discord_general`
- `discord_ngo`
- `telegram`

A Discord receipt records the logical destination that actually accepted the job. For one job and delivery kind, a receipt at either Discord destination satisfies that job's single Discord delivery obligation, so a later webhook-configuration change cannot cause an already delivered job to be sent to the other Discord webhook. If no Discord receipt exists, a retry resolves the currently configured destination and records the destination actually used.

The six-hour digest remains Discord-only and continues to use the general Discord webhook. Explore remains disabled in 4A. The weekly NGO digest remains a separate recurring summary outside these receipt semantics and must retain its current query, repeat, destination-fallback, CLI, and scheduler behavior.

## Delivery receipt schema and API

Add an explicit, idempotent table conceptually equivalent to:

```text
job_delivery_receipts
---------------------
job_id        TEXT NOT NULL
delivery_kind TEXT NOT NULL
destination   TEXT NOT NULL
delivered_at  TEXT NOT NULL

PRIMARY KEY (job_id, delivery_kind, destination)
```

Supported `delivery_kind` values are `immediate`, `digest`, and `explore`. Supported destinations are the three logical identifiers above. Keep these values typed and validated in application code; do not build a generic queue, event bus, or provider registry.

Notifier delivery results must expose the exact successful `(job_id, destination)` pairs. A provider exception, a returned HTTP error, or a failed per-job retry must omit that pair from the success result. Record successful pairs with one transactional `INSERT OR IGNORE` batch and a UTC `delivered_at`; the unique key must make repeated recording harmless.

Immediate delivery runs independently per configured obligation:

- Discord success plus Telegram failure records only the selected Discord destination.
- The next scan retries Telegram without sending the job to Discord again.
- Telegram success plus Discord failure behaves symmetrically.
- Both successes create both receipts.
- An unconfigured destination creates no receipt and is not treated as a successful send.

Current Discord and Telegram notifier methods swallow some per-job failures and return no success information. Phase 4A must add a small typed success result, make Discord HTTP status failures observable as failed jobs, and keep failures isolated so a bad destination or job cannot abort the scan.

External HTTP delivery and SQLite cannot share a transaction. The receipt mechanism provides per-destination idempotency for normal retries, but a process crash after the provider accepts a message and before its receipt commits can still produce an at-least-once duplicate. Document this narrow crash window rather than adding a message broker or claiming impossible exactly-once delivery.

## Legacy `notified` migration semantics

Retain `jobs.notified` without rewriting or backfilling the jobs table. Its only historical meaning after migration is `legacy_suppressed`:

- A historical `notified=1` row is suppressed from all Phase 4 delivery queries and must not be resent after upgrade.
- It does not prove successful historical delivery to every configured channel or Discord destination; that outcome is unknowable because the old implementation had no durable per-destination result.
- Do not invent historical receipts for any channel, destination, or delivery kind.
- A historical `notified=0` row may remain pending according to its tier, missing receipt, batch ordering, and the 14-day stale policy.
- Jobs created under Phase 4A keep `notified=0`; receipts become their authoritative delivery state.
- `mark_notified()` remains legacy-only and must not be called by new immediate, digest, or explore delivery paths.

Migration and regression tests must prove repeated initialization is safe and no previously globally notified job is unexpectedly resent.

## Pending backlog, ordering, and grouped delivery

Replace the current digest query's short lookback as the pending-state mechanism. Keep delivery bounded while separating cadence, batch size, maximum useful age, and receipt state:

- The scheduled digest still runs every six hours.
- Immediate retry processing runs after every non-dry production scan, including a scan that saves no new jobs.
- Immediate and digest delivery select at most 15 jobs per destination/run in Phase 4A.
- A pending job remains eligible until 14 days after `fetched_at`; after that it is intentionally stale and no longer selected. Staleness does not fabricate a delivery receipt.
- A full batch leaves overflow pending for the next run; it is not lost merely because it is now older than the delivery interval.
- Pending queries require `notified=0`, the matching tier, no satisfying receipt for the destination obligation, and `fetched_at` inside the stale boundary.

Use this exact deterministic ordering for immediate, digest, and future explore selections:

1. `match_score DESC`
2. `COALESCE(posted_at, fetched_at) DESC`
3. `fetched_at DESC`
4. `id ASC`

For a grouped digest, build a payload within the Discord embed limit and retain the exact IDs actually included. After a successful webhook response, record receipts only for those included jobs. Jobs excluded by the item or payload-size limit remain pending. A failed payload creates no receipts.

## Health, metrics, and compatibility

Add `explore` routing counts without removing or renaming existing fields:

- Preserve `/health` keys `immediate`, `digest`, and `diagnostic`.
- Add `explore` with a default of zero for old persisted summaries.
- Continue mapping saved `none` jobs to `diagnostic`.
- Add `explore` to per-source routing aggregation, latest-scan restoration, `--stats`, and the daily status routing block so it is never silently dropped.
- Keep `source_scan_runs.routing_counts` as JSON; no `source_scan_runs` schema migration is expected.
- Keep deployment health consumers that read only immediate/digest fields compatible.

Dry-run and explain modes must create no database, receipt, notification, metric, or other delivery-state writes.

## Expected files and modules

- Tier and routing metrics: `models/job.py`, `models/scan.py`
- Receipt migration, pending queries, and transactional receipt writes: `storage/database.py`
- Typed delivery success contract and destination-aware sends: `notifiers/base.py`, `notifiers/discord_notifier.py`, `notifiers/telegram_notifier.py`
- New focused delivery orchestration and grouped-payload module: `notifiers/delivery.py`
- Scan/digest orchestration and additive presentation: `main.py`, `health.py`
- New focused tests: `tests/v2/test_notification_delivery.py`
- Focused compatibility updates: `tests/v2/test_observability.py`, `tests/v2/test_employment_storage_presentation.py`, and existing historical digest/weekly tests where required
- Progress only: `IMPLEMENTATION_PLAN.md`

Do not add a production dependency or another service.

## Tasks

- [x] Add the four-value notification tier while preserving 70/45/none assignment and leaving explore disabled.
- [x] Add the idempotent `job_delivery_receipts` migration and typed delivery-kind/destination contracts.
- [x] Implement the exact general/NGO Discord selection and fallback rules with one Discord obligation per job, plus independent Telegram delivery.
- [x] Return exact per-job/per-destination successes from notifier sends and record only successful receipts transactionally.
- [x] Replace new delivery paths' global `notified` writes with receipt-based pending/retry logic; keep `notified` and `mark_notified()` legacy-only.
- [x] Replace the digest lookback with bounded receipt-based carry-over, deterministic ordering, and 14-day staleness.
- [x] Record grouped digest receipts for exactly the jobs included in a successful payload and keep overflow pending.
- [x] Add explore routing counts additively across metrics, persistence restoration, health, stats, and daily status.
- [x] Preserve dry-run immutability, current routing thresholds, Discord destinations, digest cadence/channel, and weekly NGO behavior.
- [x] Add the complete migration, receipt, partial-failure, backlog, compatibility, and safety test matrix.

## Acceptance criteria

- Routing tier and delivery state are independent; `explore` validates but receives no production jobs.
- A general immediate job uses only `discord_general`; an NGO immediate job uses only `discord_ngo` when configured and otherwise only `discord_general`; Telegram remains independent.
- Ordinary retries never resend a job to a destination already represented by a satisfying receipt, including across later Discord webhook-configuration changes.
- Partial destination and per-job failures create receipts only for successes, and later runs retry only missing obligations.
- Historical `notified=1` rows are conservatively suppressed without guessed receipts; historical outcomes remain explicitly unknown.
- Receipt migration and writes are idempotent, and duplicate receipt attempts leave one row.
- Digest overflow remains pending beyond the six-hour window until delivered or 14 days stale; ordering is exact and stable.
- Successful grouped delivery records only included jobs; a failed send records none.
- `/health` remains backward compatible and exposes additive explore counts while diagnostic retains saved-none semantics.
- Weekly NGO digest query/repeat behavior and all 70/45 notification tier boundaries remain unchanged.
- No new service, queue, production dependency, or Playwright/Chromium requirement is added.

## Verification

Run in this order:

```bash
python -m pytest tests/v2/test_notification_delivery.py tests/v2/test_observability.py tests/v2/test_employment_storage_presentation.py -q
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase4a .
python main.py --dry-run --source arbeitnow
python main.py --dry-run --explain
```

- The blocking v2 suite must pass. Compare the historical diagnostic with the Phase 3 reference of 1,174 passed and 104 known failures and introduce no new failing node IDs.
- Use mocked provider calls for automated delivery tests; do not send test jobs to real Discord or Telegram destinations.
- Run an isolated ephemeral 512 MiB service with temporary database/log mounts, no real `.env`, all notification/status/Zoho sends disabled, and loopback-only health binding.
- Validate migration from a representative pre-Phase-4 database twice, health restoration before/after a completed scan, additive explore counts, and no delivery-state mutation in dry-run mode.

## Memory criteria

- Peak observed container memory must remain below 430 MiB using the Phase 3 startup-scan method (reference: 352.2 MiB and approximately 91.6 seconds).
- Investigate a paired Phase 4A increase above 10 MiB or a startup-duration increase above 10% before completion.
- Pending queries and payload builders must retain only the bounded batch; receipts are bounded to the finite delivery-kind/destination combinations per job and store no payload or response body.
- Do not change source scheduling or concurrency; that remains Phase 5.

## Progress-log requirements

Record files changed, the exact receipt schema, migration and legacy-suppression proof, destination-selection behavior, focused/blocking/historical test results, Docker image, dry-run proof, `/health` compatibility, startup duration, peak memory, limitations, and confirmation that Phase 4B/5 were not started.

## Definition of done

- Every Phase 4A task and acceptance criterion is verified and recorded.
- Delivery retries are receipt-driven per logical destination, historical notified rows are safe, and backlog cannot be silently stranded by the digest interval.
- Current tier thresholds, exact Discord routing, Telegram independence, digest behavior, weekly NGO semantics, employment compatibility, company cap, and source schedules remain unchanged.
- Focused and blocking tests, historical compatibility, Docker build, dry scans, isolated health, migration, and memory checks pass.
- Commit Phase 4A separately as `feat: make job notification delivery idempotent` and stop for review before Phase 4B.

---

# Phase 4B — Recall policy, explore digest, and company-cap tuning

## Objective

Use the Phase 4A receipt foundation to expose more hard-eligible jobs through evidence-selected thresholds, a bounded daily explore digest, employment-aware presentation, a configurable tier-preserving company cap, and an explicit freelance-permission routing policy.

Phase 4B must not change hard eligibility, employment compatibility, match-score calculation, source scheduling/concurrency, or weekly NGO digest semantics. It must not begin Phase 5.

## Read-only threshold and cap simulation

Before changing production thresholds or the company-cap default, fetch the current configured sources once and run a read-only simulation over jobs that pass every hard gate and receive a match score, before company-cap rejection. The simulation must not initialize or write the production database, persist scan metrics or receipts, send notifications, advance mail state, or write ATS discovery candidates.

Report these pre-cap aggregates without descriptions or other unbounded posting content:

- score bands: 70–100, 45–69, 30–44, 15–29, and 0–14
- total hard-eligible jobs
- current score-only cap-2 rejections
- source distribution
- employment-relationship distribution
- work-schedule distribution
- `freelance_permission_required` count
- per-company counts and concentration

Simulate at least:

- A — current: immediate 70, digest 45, no explore, score-only company cap 2
- B — original proposal: immediate 70, digest 30, explore 15, tier-preserving diversity cap 5
- C — conservative alternative: immediate 70, digest 45, explore 30, tier-preserving diversity cap 5

For every scenario compare immediate, digest, explore, and none volumes; jobs hidden by the cap; jobs per company; part-time/fixed-term/contract/freelance visibility; and selected-score distribution. Compare the diversity policy with score-only selection under the same thresholds/cap and prove that it never reduces the number of selected higher-tier jobs in favor of a lower tier. Report same-tier score trade-offs separately.

Keep `immediate_score=70`. Adopt the 30/15 thresholds only when the evidence shows acceptable relevance and bounded volume; otherwise use the conservative 45/30 policy. Choose the smallest company cap from 2 through 5 that materially improves distinct actionable employment visibility without unacceptable company concentration; retain 2 when evidence is inconclusive. Record the simulation, chosen defaults, and rationale before final rollout rather than mechanically adopting either proposal.

Add a read-only CLI diagnostic such as `python main.py --simulate-notifications` so the same aggregate comparison can be repeated during tuning. This mode must share production hard-gate/scoring logic but bypass persistence, notifications, discovery writes, and delivery state.

## Typed notification policy

Load one validated `NotificationPolicy` from `profile.toml`; do not scatter score, cap, item-limit, stale-age, or freelance-routing decisions across `main.py`, storage, and notifier renderers. The policy must contain:

```toml
[notifications]
immediate_score = 70
digest_score = 45                 # final value selected by simulation
explore_score = 30                # final value selected by simulation
daily_explore_enabled = true
explore_hour_utc = 17
immediate_max_items = 15
digest_max_items = 15
explore_max_items = 10
pending_max_age_days = 14
max_jobs_per_company = 2          # final value selected from 2..5 by simulation
freelance_permission_max_tier = "digest"
```

The shown digest/explore/cap values are the conservative fallback when evidence is inconclusive, not a substitute for running the simulation. Validate:

- `100 >= immediate_score > digest_score > explore_score >= 0`
- enablement is boolean
- UTC hour is an integer from 0 through 23
- all item limits are positive bounded integers no greater than 25
- pending age is a positive bounded integer no greater than 30
- company cap is an integer from 1 through 5
- freelance ceiling is one of `immediate`, `digest`, or `explore`

Configuration errors must fail clearly at profile load. Score assignment, pending queries, delivery scheduling, company selection, and simulation must all consume this same typed policy.

## Explore and digest delivery

Assign tiers only after hard eligibility and scoring:

- score at or above immediate threshold → `immediate`
- otherwise score at or above digest threshold → `digest`
- otherwise score at or above explore threshold and explore enabled → `explore`
- otherwise → `none`

Explore contains only hard-eligible jobs, never sends immediate alerts, and runs once daily at the configured UTC hour. It uses the Phase 4A receipts, deterministic pending ordering, 14-day stale policy, Discord-general grouped delivery, and a strict default limit of 10. A failed delivery stays pending for only the missing logical destination; successful or stale jobs do not repeat.

Keep the regular digest Discord-general only and bounded to 15 by default. Digest and explore payload builders must carry overflow deterministically, respect channel size limits, return exact included IDs, and record receipts only after successful payloads.

## Tier-preserving company-cap diversity

Make the company cap configurable through `NotificationPolicy`. Assign every hard-eligible candidate its policy-derived tier, including the freelance-permission ceiling, before final company-cap selection.

Classify each candidate into exactly one mutually exclusive employment bucket with this precedence:

1. `freelance` when `employment_relationship == "freelance"`
2. `part_time` when the non-freelance job has `work_schedule == "part_time"`
3. `contract_or_fixed_term` when the remaining job is a contract employee or fixed term
4. `standard` for every remaining job

Within each normalized company, preserve routing priority exactly:

1. Process tiers in the order `immediate`, `digest`, `explore`, `none`.
2. A lower-tier candidate can never consume a slot while an unselected higher-tier candidate remains.
3. Within the current tier, keep its highest-ranked job first.
4. When more slots remain in that same tier, prefer the highest-ranked candidates from useful employment buckets not yet represented in that tier, using the bucket precedence above.
5. Fill remaining slots from that tier by deterministic rank before considering the next tier.
6. Never exceed the one overall company limit; do not create a separate cap per tier or employment category.

Rank candidates by match score descending, `posted_at` or `fetched_at` recency descending, `fetched_at` descending, and ID ascending. This must produce the documented behavior:

- With cap 2, two full-time immediate jobs at 82 and 75 both beat a freelance explore job at 35; diversity cannot replace the 75 immediate job.
- With cap 2, immediate jobs at 82 full-time, 77 part-time, and 75 full-time select 82 and 77 because diversity is applied within the same immediate tier.

Every non-selected candidate receives exactly one terminal `company_cap` rejection. Overall and per-source `raw == accepted + sum(primary rejection counts)` must remain true.

## Freelance-permission policy and presentation

Use the existing `freelance_permission_required` marker without changing employment compatibility or rejecting the job. Apply the configured maximum tier after score-based routing and before company-cap selection. The default `digest` ceiling demotes an otherwise immediate permission-required freelance role to digest while leaving existing digest/explore/none results unchanged. Other freelance jobs remain score-routed.

Keep the permission warning visible. Group digest/explore jobs into compact, mutually exclusive presentation sections:

- strong employee/standard roles
- part-time, contract-employee, and fixed-term roles
- freelance roles, with the permission warning where applicable

A job appears once per delivery and once across sections. Within each section preserve deterministic delivery order, bound titles/details, and keep every Discord payload within provider limits.

## Health, metrics, and compatibility

- Use the additive Phase 4A explore routing count across current scan metrics, persisted JSON restoration, `/health`, `--stats`, and daily status.
- Keep `diagnostic` mapped only from saved `none` jobs.
- Preserve existing source/funnel accounting and deployment-facing immediate/digest fields.
- Require the simulation and normal dry-run paths to remain immutable.
- Do not migrate `source_scan_runs`, change notification credentials, or alter weekly NGO repeat behavior.

## Expected files and modules

- Typed policy and tier assignment: `filters/profile.py`, `filters/match.py`, `profile.toml`
- Tier-preserving diversity selection and terminal accounting: `filters/pipeline.py`
- Read-only simulation and explore scheduling: `main.py`
- Pending-query policy parameters and receipts: `storage/database.py`
- Grouped delivery orchestration and compact employment-section rendering: `notifiers/delivery.py`, with Discord transport in `notifiers/discord_notifier.py`
- New focused tests: `tests/v2/test_notification_policy.py`
- Focused regressions: `tests/v2/test_notification_delivery.py`, `tests/v2/test_observability.py`, and employment/pipeline tests
- Progress only: `IMPLEMENTATION_PLAN.md`

Do not add a production dependency or another service.

## Tasks

- [x] Add the immutable aggregate simulation and record A/B/C evidence before choosing thresholds or cap defaults.
- [x] Add and validate the centralized `NotificationPolicy`; route all notification decisions through it.
- [x] Assign immediate/digest/explore/none tiers at exact boundaries and apply the configured freelance-permission ceiling without rejection.
- [x] Replace the hardcoded cap with the tier-preserving, diversity-aware, one-overall-limit selector.
- [x] Add the bounded daily Discord-general explore digest using Phase 4A receipts, retry, ordering, carry-over, and staleness.
- [x] Keep the regular digest bounded and receipt-driven; keep immediate alerts concise and high precision.
- [x] Add compact mutually exclusive employment sections and visible freelance-permission warnings without duplicate jobs.
- [x] Preserve source/funnel accounting, health compatibility, dry-run immutability, and weekly NGO behavior.
- [x] Add the complete threshold, configuration, cap, delivery, presentation, accounting, and performance test matrix.

## Acceptance criteria

- Simulation reports every required aggregate and scenario without a database or notification-side effect, and the progress log records why the final defaults were chosen.
- Threshold boundaries are exact; immediate remains at 70 and no explore job generates an immediate alert.
- The company selector never replaces a higher-tier job with a lower-tier diversity candidate and preserves the number of selected higher-tier jobs relative to score-only selection under the same cap.
- Same-tier diversity follows the documented examples and deterministic bucket precedence; the one overall cap is never multiplied by category or tier.
- Every cap exclusion has exactly one terminal rejection and all funnel invariants hold overall and per source.
- Part-time, fixed-term, contract-employee, and freelance roles gain visibility when comparable roles exist in the same tier, without uncontrolled company flooding.
- Permission-required freelance jobs remain eligible, are capped at digest by default, retain the warning, and follow configuration when the ceiling changes.
- Explore and digest item limits, carry-over, stale expiry, receipts, partial failures, and no-duplicate behavior remain correct per logical destination.
- Each grouped job appears exactly once, messages remain within provider limits, and Phase 4A health/routing compatibility remains intact.

## Test matrix

Automated tests must cover at least:

- threshold boundary values and invalid ordering/types/bounds
- explore enabled/disabled assignment, none assignment, and unchanged immediate precision
- simulation output fields, A/B/C counts, cap rejection reporting, and complete write/notification immutability
- company-cap configuration, exact tier priority, both documented cap-2 examples, deterministic bucket assignment/precedence, deterministic tie-breaking, and one terminal rejection
- proof that lower-tier diversity never displaces higher-tier jobs and higher-tier counts match score-only selection under the same cap
- part-time/freelance visibility within a tier and no per-category/tier cap explosion
- immediate, digest, and daily explore item limits; carry-over; 14-day expiration; and ordering
- receipt idempotency, actual Discord destination selection, partial channel failure, and retry of only missing obligations
- freelance permission ceiling for immediate/digest/explore configurations without employment rejection
- employment section formatting, provider-size bounds, and no duplicate job across sections
- existing source/funnel/routing accounting, health restoration, weekly NGO behavior, and dry-run immutability

## Verification

Run focused policy, delivery, pipeline, presentation, and observability tests first, then:

```bash
python -m pytest tests/v2/test_notification_policy.py tests/v2/test_notification_delivery.py tests/v2/test_observability.py -q
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
python main.py --simulate-notifications
docker build -t job-bot:phase4b .
python main.py --dry-run --source arbeitnow
python main.py --dry-run --explain
```

- The blocking v2 suite must pass. The historical diagnostic must introduce no failing node IDs beyond the verified 104 known failures.
- Automated provider/delivery tests must use mocks; live source simulation remains diagnostic and must not determine unit-test success.
- Run an isolated ephemeral 512 MiB service with temporary database/log mounts, no real `.env`, disabled real sends/Zoho, and loopback-only health binding. Validate scheduled registration, restored/completed `/health`, receipt carry-over with seeded fixtures, and compact status output.

## Memory criteria

- Peak observed container memory must remain below 430 MiB using the Phase 3 method (reference: 352.2 MiB and approximately 91.6 seconds).
- Investigate a paired Phase 4B increase above 10 MiB or a startup-duration increase above 10% before completion.
- Simulation, cap selection, pending queries, and renderers must operate on bounded/current scan data without retaining response bodies or unbounded payload/history.
- Keep SQLite and existing conservative source concurrency; do not optimize source scheduling here.

## Progress-log requirements

Record files changed, the complete simulation table and selected defaults, threshold/cap rationale, final policy values, company-tier/diversity comparison, freelance-ceiling behavior, focused/blocking/historical tests, Docker image, dry-run proof, health output, startup duration, peak memory, limitations, and confirmation that Phase 5 was not started.

## Definition of done

- Every Phase 4B task and acceptance criterion is verified and recorded using the approved Phase 4A foundation.
- Lower-scoring hard-eligible jobs are visible only through bounded policy-driven delivery, while immediate precision and tier priority are preserved.
- Company diversity operates only within routing priority, never multiplies the cap, and retains exact terminal accounting.
- Focused and blocking tests, historical compatibility, simulation, Docker build, dry scans, isolated health, receipt retries, and memory/startup checks pass.
- Commit Phase 4B separately as `feat: add bounded explore routing and recall tuning` and stop for review before Phase 5.

---

# Phase 5A — Grouped Scheduler, Locking, Health, and Bounded Source HTTP

**Status:** Completed

## Objective

Replace the single immediate all-source scheduler job with measured, staggered source groups and one coordinated production scan lifecycle. Make source-fetch concurrency predictable without serializing large ATS board sets, keep the service ready while source refreshes run asynchronously, preserve Phase 1–4 behavior, and remain below 430 MiB.

Phase 5A must not enable impact sources, repair StepStone, change hard eligibility, add a cross-scan company quota, or begin Phase 6. Stop for review after the Phase 5A commit and verification.

## Verified planning baseline and source economics

Start from the verified Phase 4B reference:

- blocking v2: 394 passed
- historical diagnostic: 1,271 passed and 104 known failures
- peak memory: 344.2 MiB, below the 430 MiB hard target
- current-network all-source startup scan: approximately 206 seconds
- paired unchanged Phase 4A under the same network conditions: approximately 191 seconds
- Phase 4B increase itself: 7.9%
- one scheduled all-source scan every 45 minutes, started immediately
- outer scan batching and most inner board fan-out both use `MAX_CONCURRENT_SOURCES=3`
- several adapters have independent or nested query/detail fan-out, so the setting is not a unified request ceiling

A bounded live planning audit on 2026-08-07 produced the following aggregate evidence. No descriptions or provider payloads were persisted or committed. Default-source final counts use the current combined all-source company cap; impact-source final counts use the candidate impact set.

| Source | Status | Raw | Hard-eligible pre-cap | Final accepted | Duration | Fan-out / issue | Phase 5 decision |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Greenhouse | healthy | 6,216 | 19 | 15 | 48.7s | 38 boards; one recovered transient request | Group A |
| Ashby | healthy | 3,069 | 16 | 4 | 18.7s | 78 boards | Group A |
| Personio | partial_success | 1,397 | 8 | 7 | 7.0s | 72 boards plus nested HTML-detail fan-out; one Pitch redirect issue | Group A |
| Lever | healthy | 472 | 1 | 1 | 6.7s | 8 boards | Group A |
| Workable | healthy | 9 | 0 | 0 | 0.3s | 3 boards | Group A |
| JSON-LD | zero_results | 0 | 0 | 0 | <0.1s | no configured live board; negligible scan cost | Group A |
| Arbeitnow | healthy | 175 | 1 | 1 | 1.0s | one request | Group B |
| StepStone | blocked | 0 | 0 | 0 | 0.6s | all five queries failed with HTTP 403; prior phases had five HTTP 404 failures | manual-only / suspended |
| Remotive | healthy | 33 | 0 | 0 | 1.1s | three category requests | Group B |
| Himalayas | healthy | 17 | 0 | 0 | 2.1s | up to ten sequential pages | Group B |
| RemoteOK | healthy | 100 | 0 | 0 | 1.0s | one request | Group B |
| Idealist | healthy | 35 | 0 | 0 | 0.7s | two Algolia queries; current direct `httpx` path requires budgeting | Group B |
| LinkedIn | healthy | 38 | 30 | 27 | 0.7s | nine guest queries | Group B |
| GoodJobs | zero_results | 0 | 0 | 0 | 0.8s | approximately 750 KB of HTML produced no parsed jobs | Phase 5B manual-only baseline |
| ReliefWeb | healthy | 28 | 0 | 0 | 1.2s | three feeds; all 28 rejected on location | Phase 5B manual-only baseline |
| Devex | healthy | 20 | 0 | 0 | 0.9s | up to two pages; 19 location and one role rejection | Phase 5B manual-only baseline |
| EuroBrussels | healthy | 29 | 0 | 0 | 1.1s | two pages; all 29 rejected on location | Phase 5B manual-only baseline |
| 80,000 Hours | healthy | 44 | 0 | 0 | 0.7s | three pages after 255 adapter-prefilter exclusions; 37 location and seven role rejections | Phase 5B manual-only baseline |
| Tech Jobs for Good | blocked | 0 | 0 | 0 | 0.2s | Cloudflare HTTP 403 | Phase 5B manual-only baseline |

The default audit returned 11,561 raw jobs, 75 hard-eligible pre-cap jobs, and 55 final accepted jobs. LinkedIn contributed 30 of 75 pre-cap and 27 of 55 final jobs, so it remains scheduled while its guest source stays healthy. The impact candidates returned 121 raw jobs and no hard-eligible jobs.

Measured raw content-hash overlap was Ashby/Lever 32, Ashby/Arbeitnow 24, and Ashby/RemoteOK one; no URL-ID overlap was found. Applying the existing scan-local company cap separately to Groups A and B selected the same 55 jobs as the combined scan, introduced no selected cross-group duplicates, added no jobs, and left every company at no more than two selections. The planning diagnostic peaked at approximately 341 MiB, while the Phase 4B production-like 344.2 MiB result remains the authoritative memory reference.

## Source catalog and group configuration

Add one focused source catalog separate from the existing employer-board registry. `sources/registry.py` already owns `companies.toml` board definitions and must not import adapter classes; use a distinct focused module such as `sources/catalog.py` to avoid circular imports.

The catalog must expose:

- source name to adapter class
- scheduled group or manual-only state
- stable group ID, cadence, and startup delay
- the deterministic union of enabled scheduled sources used by manual all-source production scans
- any reviewed exception to the shared source HTTP budget, with a required bounded rationale

Scheduled membership:

### Group A — direct employer / ATS boards

Candidate cadence: 60 minutes

- greenhouse
- ashby
- personio
- lever
- workable
- jsonld

### Group B — aggregators / broad discovery

Candidate cadence: 120 minutes

- arbeitnow
- remotive
- himalayas
- remoteok
- idealist
- linkedin

### Group C — impact / NGO discovery

Candidate cadence: 360 minutes, but do not register this scheduler job until Phase 5B admits at least one source.

StepStone remains registered but manual-only and suspended from scheduling pending repair or Phase 6 alert ingestion. Preserve its adapter, fixture/tests, source-health support, and `python main.py --dry-run --source stepstone`. Do not repair it in Phase 5A.

Keep all other optional adapters registered and manually addressable. A source may appear in at most one scheduled group. `python main.py --dry-run --source NAME` must continue resolving every registered adapter, and an unqualified manual production scan must run the union of enabled scheduled groups.

Make group cadences and measurement-selected startup delays configurable through scheduling-specific runtime settings rather than candidate matching fields in `profile.toml`. Do not build a generic plugin framework.

## Concurrency contracts

Introduce three separately validated controls:

- `MAX_CONCURRENT_SOURCE_ADAPTERS`: maximum source adapters concurrently active inside one source scan
- `MAX_CONCURRENT_SOURCE_COMPONENTS`: the per-fan-out-boundary component concurrency value
- `MAX_CONCURRENT_HTTP_REQUESTS`: the scan-wide source HTTP request ceiling

`MAX_CONCURRENT_HTTP_REQUESTS` is the authoritative absolute ceiling for simultaneous source network attempts participating in one `run_scan()` lifecycle. It is not a process-wide HTTP limit and does not govern Discord delivery, Telegram delivery, Zoho Mail, health endpoints, or other non-source integrations. Because the production scan coordinator allows only one production source scan at a time, it provides a predictable global ceiling for source-fetch requests during production scanning without constraining unrelated process HTTP.

If `MAX_CONCURRENT_SOURCES` is retained for deployment compatibility, treat it only as a deprecated fallback for source-adapter concurrency. It must no longer control component fan-out or the scan-wide source HTTP budget. Do not set it to one as a production target.

## Safe component fan-out and deadlock prevention

Do not implement `MAX_CONCURRENT_SOURCE_COMPONENTS` as one recursively acquired semaphore shared by outer and nested component work.

No component task may hold a shared component permit while awaiting nested tasks that require the same permit. In particular, Personio must never enter this deadlock shape:

```text
outer board holds shared component permit
  -> board awaits detail tasks
     -> detail tasks require the same exhausted permit
```

Apply these rules:

- each board, query, page, or detail fan-out boundary owns an independent local limiter or bounded worker pool
- an outer component may await a nested boundary only when the nested boundary does not acquire the outer boundary's permits
- use bounded batches or fixed-size workers so limiting HTTP does not create an unbounded collection of waiting tasks
- nested fan-out remains bounded by explicit local limits
- the scan-wide source HTTP request budget remains the final ceiling on simultaneous source network attempts across all adapters and nested boundaries
- component and HTTP permits release on success, exception, and cancellation

Refactor common bounded mapping as necessary so a large ATS list creates only bounded live worker tasks rather than one task per board waiting on a semaphore.

## Scan-wide source HTTP request budget and network-path audit

Create one shared source-request budget per scan and bind it to every participating source. Acquire it around each source HTTP attempt in the common GET/POST transport and release it before retry backoff. Share the same fetch orchestration between production scans and read-only notification simulation.

Before claiming the ceiling is complete, audit every scheduled source for source-side network paths that bypass `BaseSource._get()` or `_post()`, including:

- direct `httpx`
- direct `aiohttp`
- `requests`
- `urllib`
- feed/parser APIs given a URL rather than already-fetched content
- adapter-specific clients or SDKs

The current repository inspection found Idealist's direct `httpx.AsyncClient` path; route it through the common budgeted transport. Re-audit every scheduled adapter rather than assuming Idealist is the only bypass. Every scheduled source-side request capable of running during `run_scan()` must either participate in the shared source budget or be declared as a reviewed, bounded exception in the source catalog.

Add a practical audit assertion that enumerates scheduled adapter modules and rejects known direct-network constructs outside the shared source transport unless the catalog contains a non-empty reviewed exception. This assertion must make a newly scheduled adapter's silent source-budget bypass fail tests where practical.

Do not route notifier, Zoho, health, or other non-source integration HTTP through this semaphore.

## Paired concurrency experiment

Do not choose production concurrency values from theory. Compare two paired passes, ordered A/B/C and then C/B/A under closely matched network conditions:

| Configuration | Source adapters | Components per boundary | Scan-wide source HTTP ceiling |
| --- | ---: | ---: | ---: |
| A — unchanged Phase 4B image | 3 | current mixed behavior | no unified ceiling |
| B — lower bounded | 2 | 3 | 4 |
| C — bounded parallel | 3 | 3 | 6 |

For Groups A and B record:

- normal/representative duration
- observed slow/outlier duration
- peak container memory sampled throughout the run
- configured source HTTP ceiling and observed source-request peak for B/C
- source failures, partial failures, retries, and rate limits
- raw, hard-eligible pre-cap, and final accepted counts

Prefer B when it stays below 430 MiB, introduces no source-health or useful-coverage regression, and representative Group A/B durations remain within 20% of the paired baseline. Otherwise select C under the same criteria. Investigate any missing high-yield source or unexplained raw-count reduction above 5%. Stop and report if neither bounded configuration qualifies.

Record the selected production values only after this experiment.

## Grouped APScheduler jobs and startup staggering

Replace the single scheduled job `scan` with stable group IDs such as `source_group_a`, `source_group_b`, and conditional `source_group_c`.

Set every non-empty source-group job explicitly to:

- `max_instances=1`
- `coalesce=True`
- bounded `misfire_grace_time`
- a non-immediate initial trigger

Do not use `next_run_time=now` for every group. Select exact startup offsets after the concurrency experiment using representative normal durations plus a reasonable safety buffer:

- Group A: first non-zero whole minute after core readiness
- Group B: a bounded later offset based on representative Group A duration plus approximately two minutes
- Group C: similarly staggered after Group B when enabled

Validate that configured offsets are non-zero, distinct, inside their group's cadence, and useful for normal-operation staggering. Do not derive offsets from one pathological timeout/retry run, require that offsets prevent every possible wall-clock overlap, or reject an otherwise valid schedule because an outlier lasted longer than its offset. Record both representative and slow/outlier durations.

The production scan coordinator is authoritative overlap protection. If Group A overruns into Group B's trigger, Group B waits once; no concurrent production scan starts. `max_instances=1` and coalescing prevent repeated instances of the same group from accumulating.

## Production scan coordinator and overlap semantics

Add one shared in-process coordinator covering the complete non-dry production `run_scan()` lifecycle:

- source fetching
- ATS discovery
- filtering and scan-local company cap
- database deduplication and persistence
- source metrics and health publication
- pending immediate delivery after persistence

Scheduled groups wait FIFO for the coordinator. Because each group has `max_instances=1`, at most one instance per different group can wait or run; later same-group triggers do not create an unbounded queue. After a long provider outage, the active scan completes and each already-waiting different group runs once.

Discord and Telegram manual production scans do not queue. They return a clear busy response containing only the active scan scope and start time. An idle manual production scan runs the union of enabled scheduled groups with scope `manual_all`.

Release coordinator state on success, exception, and cancellation. Dry-run CLI processes remain outside this in-process coordinator.

## Core service readiness

`ready=true` represents core local service readiness, not successful connectivity to every optional external system.

Core readiness requires:

1. SQLite initialized and migrated.
2. Persisted health restored.
3. `/health` listening.
4. Source-group scheduler registered and started.
5. Local notification/command workers or supervised tasks initialized or scheduled sufficiently for the process to operate.

Core readiness must not wait for a successful connection or response from Discord, Telegram, Zoho, or job sources. Optional connection attempts must run through supervised background work; failures are logged and may appear as bounded operational/degraded integration state, but they do not block or reset core readiness.

Use deterministic lifecycle semantics:

```text
process start -> ready=false
local core startup completes -> ready=true
source refresh and optional external connections continue asynchronously
orderly shutdown -> ready=false
```

Telegram API initialization, Discord connection problems, Zoho unavailability, and source-provider failures must not leave the otherwise operational service unready indefinitely. Record time to core readiness, each group duration, and time until all enabled groups have completed one refresh.

## Grouped health and SQLite migration

Add an explicit, idempotent `scan_scope TEXT NOT NULL DEFAULT 'legacy_all'` column to `source_scan_runs` and an index supporting latest completion by scope. Persist stable values such as `group_a`, `group_b`, `group_c`, and `manual_all`; do not infer scope from source membership because membership can change.

Preserve every deployment-facing health field. Define:

- `last_scan_summary` legacy counts as the most recently completed production scope
- additive `last_scan_summary.scope`
- `last_scan_summary.sources` as only the sources attempted in that scope
- compact last-completion timestamps for each scheduled group
- per-source latest health as authoritative across every group and cadence
- no synthetic zero, failure, stale state, or timestamp change for sources absent from the current group
- top-level `last_scan` as the latest production completion
- `next_scan_in_seconds` as the nearest next source-group trigger
- additive core readiness state and timestamp without changing legacy `status` semantics

Restore scope and group completion state from SQLite before a new source refresh completes. Daily status must label the latest scan scope and show compact group freshness so a Group C-only scan cannot be presented as all-source coverage. Optional integration or provider degradation must not reset core readiness.

## Company cap, notification, and discovery compatibility

Keep the Phase 4B company cap scan-local. Grouped scans therefore apply the cap separately. The planning audit found no additional selected jobs or company above two across separate Groups A/B, but repeat this comparison after implementation. Do not add a cross-window database quota in Phase 5A. If new evidence shows unacceptable employer flooding beyond what current batch limits control, stop and report rather than inventing a new policy.

Every group production scan must continue to:

- save only rows actually inserted
- update metrics for attempted sources only
- process pending immediate delivery afterward
- use Phase 4A delivery receipts and Phase 4B notification policy
- leave digest, explore, and weekly NGO cadence independent

URL/content deduplication and receipts must prevent repeated persistence and delivery when multiple groups discover the same posting.

Preserve ATS discovery scope and existing append deduplication. Group A direct-source jobs produce no aggregator discovery candidates; Group B may append a newly discovered candidate only once. Dry-run and notification simulation must not write jobs, metrics, receipts, health history, ATS discovery data, or mail state.

Production remains free of Playwright and Chromium through dependencies, Docker architecture, and tests. Do not add a dead `DISABLE_PLAYWRIGHT` setting.

## Tasks

- [x] Add the focused source catalog, exact Group A/B membership, manual-only state, cadence validation, and conditional Group C omission.
- [x] Replace outer and component use of `MAX_CONCURRENT_SOURCES` with independently validated adapter, per-boundary component, and scan-wide source HTTP settings.
- [x] Add deadlock-safe local component fan-out with bounded worker/task creation, including nested Personio detail work.
- [x] Add the shared per-scan source HTTP budget and audit every scheduled adapter network path; route Idealist and all other unapproved bypasses through it.
- [x] Add the scheduled-adapter bypass assertion and explicit reviewed-exception contract.
- [x] Run the paired A/B/C experiment before selecting production concurrency or startup offsets.
- [x] Register stable, staggered group jobs with explicit `max_instances=1`, coalescing, and bounded misfire behavior.
- [x] Add the shared production scan coordinator with scheduled FIFO waiting and manual busy behavior.
- [x] Separate core readiness from group refresh and optional external connectivity.
- [x] Add the idempotent `scan_scope` migration and group-aware persisted/active health semantics.
- [x] Preserve scan-local company caps, receipts/policy, deduplication, ATS discovery dedup, and dry-run/simulation immutability.
- [x] Record representative/outlier durations, readiness, first full refresh, group memory, and maximum staggered-operation memory.

## Test matrix

Automated tests must use mocked external HTTP and cover at least:

- source catalog membership, no unintended multi-group membership, manual-only resolution, and cadence validation
- Group C omission when empty
- stable scheduler IDs, non-zero/distinct/in-cadence startup offsets, `max_instances=1`, coalescing, and misfire grace
- separate adapter and per-boundary component concurrency
- nested Personio-like fan-out completing without deadlock with a small component limit
- bounded component live-task creation
- observed source HTTP concurrency never exceeding `MAX_CONCURRENT_HTTP_REQUESTS`
- component and source HTTP permits released on success, exception, and cancellation
- every scheduled source network path budgeted or explicitly excepted
- scheduled/scheduled FIFO waiting, scheduled/manual busy behavior, and no concurrent production `run_scan()`
- coordinator release after failure and cancellation
- source-outcome isolation
- readiness true before the first group scan
- unavailable/mocked Discord and Telegram connectivity not preventing core readiness
- source-provider failure not resetting readiness
- deterministic `ready=false -> true -> false` across startup/shutdown and fresh restart
- idempotent scope migration, legacy defaults, persisted restoration, and latest-scope semantics
- per-source health/timestamps across different group cadences
- `next_scan_in_seconds` from the nearest group trigger and daily status scope/freshness
- notification receipt/dedup regressions across groups
- unchanged scan-local company-cap accounting
- ATS discovery append deduplication
- dry-run and simulation immutability
- no Playwright/Chromium dependency, import, process, or dead setting

## Verification and performance

Run focused Phase 5A tests first, then:

```bash
python -m pytest tests/v2 -q
python -m pytest -q --timeout=30
python -m pytest tests -q --timeout=30
docker build -t job-bot:phase5a .
```

The blocking v2 suites must pass. The historical diagnostic must introduce no failing node IDs beyond the verified 104. Run bounded live diagnostics for every Group A/B source, StepStone, and LinkedIn; live providers remain non-blocking and must not be dependencies of automated tests.

Run the paired A/B/C experiment, an isolated service-readiness check, `/health` restoration, scheduled/manual overlap tests, and one staggered first refresh inside a 512 MiB container. Record:

- time to core readiness
- normal and slow/outlier Group A/B duration
- peak memory for each group and across staggered operation
- observed source HTTP peak and configured ceiling
- time until all enabled groups complete one refresh
- raw, pre-cap, final, status, issue, retry, and rate-limit comparisons

Peak memory must stay below 430 MiB. Investigate any new source instability, unexplained coverage loss, recursive fan-out deadlock, unbudgeted scheduled-source network path, or meaningful latency regression before completion.

## Definition of done

- Group A/B run on separate measured, staggered schedules; Group C is absent.
- Only one production source scan can run, with bounded FIFO scheduled waiting and non-queued manual scans.
- Nested component fan-out is deadlock-safe and task-bounded.
- All scheduled source network attempts are covered by the scan-wide source HTTP ceiling or an explicit reviewed exception.
- Core readiness is available before source refresh and independent of optional external connectivity.
- Health remains backward compatible, group-aware, persisted, and authoritative per source.
- Phase 1 employment/source health, Phase 2 employment, Phase 3 language, and Phase 4 policy/receipts remain intact.
- Focused/blocking tests, historical compatibility, Docker build, live diagnostics, the concurrency experiment, readiness, `/health`, overlap, and memory checks pass.
- Commit Phase 5A separately as `feat: group and bound production source scans` and stop for review before Phase 5B.

---

# Phase 5B — Impact-Source Admission

**Status:** Completed

## Objective

Evaluate existing impact/NGO adapters under the Phase 5A scheduler, source HTTP budget, health, and memory behavior. Enable only sources that pass the admission gate. Phase 5B may complete with no sources enabled and must not begin Phase 6.

## Admission gate

An adapter is not scheduled merely because it exists. Each candidate must demonstrate:

- technical health suitable for unattended operation
- no browser runtime, authentication, or unsafe scraping requirement
- bounded source HTTP behavior covered by the Phase 5A scan-wide source HTTP ceiling or an explicit reviewed exception
- bounded component/task fan-out without recursive limiter deadlock
- valid title, company, location, and URL normalization
- accurate source outcome and partial-success reporting
- useful and explicit Germany/EU/EMEA/worldwide or Berlin eligibility evidence
- at least some plausible relevant tech/digital roles or a defensible mission-source benefit
- acceptable scan duration and no source instability/rate-limit regression
- acceptable memory under the shared below-430 MiB target
- fixture-based automated tests with mocked HTTP

For every candidate record `enabled` or `manual-only` with the evidence and reason. Do not weaken the eligibility invariants or invent worldwide scope to produce yield.

## Current admission baseline

- **GoodJobs — manual-only:** current page returned approximately 750 KB but parsed zero jobs, indicating parser drift rather than a trustworthy zero-result run.
- **ReliefWeb — manual-only:** 28 raw jobs, all rejected on location, with insufficient Germany/Berlin remote eligibility evidence; its multi-feed outcome semantics must also pass the Phase 5A contract before admission.
- **Devex — manual-only:** 20 raw jobs, 19 location rejections and one role rejection; no current hard-eligible yield.
- **EuroBrussels — manual-only:** 29 raw jobs, all location-rejected; current on-site EU roles do not satisfy Berlin-only on-site eligibility.
- **80,000 Hours — manual-only:** 44 prefiltered development-role candidates, with 37 location and seven role rejections; unknown/restricted location evidence is not sufficient for scheduling.
- **Tech Jobs for Good — manual-only:** both current public URL attempts are blocked with HTTP 403 by Cloudflare.

Do not enable GoodJobs, ReliefWeb, or Devex merely because the previous stale plan named them. Do not repair StepStone in Phase 5B. Keep every candidate manually runnable for diagnostics.

## Tasks

- [x] Re-run bounded live dry diagnostics for all six candidates under the selected Phase 5A concurrency configuration.
- [x] Audit source-network coverage, component outcome semantics, normalization, eligibility evidence, duration, memory, and unique useful yield.
- [x] Record an explicit enabled/manual-only decision and reason for every candidate.
- [x] Add only qualifying sources to Group C; keep the Group C scheduler absent when none qualify.
- [x] Add or update mocked fixtures/tests only for a source that receives a justified adapter correction or production admission. No candidate qualified and no adapter correction was justified, so no fixture/test change was required.
- [x] Preserve manual access to all candidates regardless of scheduling decision.

## Verification and performance

Automated tests must cover the admission decision registry, conditional Group C registration, manual-only resolution, source outcome isolation, source HTTP budgeting, and all corrected adapter behavior without live dependencies.

For every candidate record live status, raw, hard-eligible pre-cap, final accepted, duration, issues, eligibility-evidence quality, and approximate unique useful yield. For any enabled Group C membership, run an isolated 512 MiB Group C scan, record peak memory/duration, verify grouped health scope, and repeat the cross-group company-cap/receipt assessment.

## Definition of done

- Every impact candidate has an evidence-backed enabled/manual-only decision.
- No source is enabled solely to satisfy a checklist or source-count target.
- Group C is registered only when at least one source passes the complete gate.
- It is valid for Phase 5B to complete with no production source enabled.
- Manual source execution, health support, hard eligibility, Phase 4 receipts/policy, and the below-430 MiB target remain intact.
- Commit Phase 5B independently if implementation or admission-state changes are warranted, record results in its progress entry, and stop before Phase 6.

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

## Phase 2A — Employment model and classification

- Status: Completed on 2026-08-07
- Commit: `feat: classify employment relationships and schedules` (this Phase 2A commit)
- Tests:
  - Focused: `python -m pytest tests/v2/test_employment.py tests/v2/test_employment_storage_presentation.py -q` — 85 passed.
  - Blocking v2: `python -m pytest tests/v2 -q` — 189 passed.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 189 passed.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30` — 1,066 passed, 104 failed; a JUnit node-ID comparison against the recorded Phase 0 list found 0 new and 0 missing failing IDs.
- Peak memory: 335.3 MiB / 512 MiB (65.49%), 10.1 MiB above the 325.2 MiB Phase 1B reference and 94.7 MiB below the 430 MiB target.
- Notes:
  - Files changed: `models/job.py`, `models/scan.py`, `filters/employment.py`, `filters/profile.py`, `filters/pipeline.py`, `filters/role.py`, `profile.toml`, `storage/database.py`, `main.py`, `notifiers/discord_notifier.py`, `notifiers/telegram_notifier.py`, `tests/v2/test_employment.py`, `tests/v2/test_employment_storage_presentation.py`, and this progress entry.
  - Added independent relationship, schedule, term, weekly-hours, duration, rate, bounded evidence, and freelance-permission fields. The shared classifier accepts partial structured inputs independently, preserves structured authority per dimension, uses title/tags before description, and leaves genuine heuristic conflicts unknown.
  - Added one validated profile-driven employment policy and one terminal `employment_relationship` gate after location and before role. Student/intern terms were removed from role configuration and the pipeline's role/seniority ownership; the standalone legacy role helper remains compatible while the global pipeline uses the employment-free role profile gate.
  - Added eight explicit idempotent SQLite columns. A representative Phase 1 database migrated twice safely; old rows reconstructed with unknown/`None`/empty/false defaults, and all independent combinations plus JSON reasons and the permission marker round-tripped.
  - CLI normal/explain output, scheduled Discord digest rows, Discord alerts, and Telegram alerts now show compact readable employment metadata and the informational freelance-permission marker while omitting unknown/empty fields.
  - Explicit routing tests proved otherwise identical employee/freelance jobs retain the same score and notification tier, and the permission marker does not suppress either Discord or Telegram delivery. Live startup routing remained 8 immediate, 31 digest, and 0 diagnostic.
  - Final image: `job-bot:phase2a`, `sha256:af0f4ef004e83a35240a126b33c032c41bdba3620b6a4ceafc4de3fef198a5b4`, 373,986,067 bytes.
  - Live Arbeitnow dry-run: 175 raw / 2 accepted. The all-default explain/diagnostic scan: 11,595 raw / 39 accepted / 11,556 rejected, including 83 employment rejections (47 internship and 36 working-student). Accepted metadata was relationship 1 freelance / 38 unknown, schedule 7 full-time / 32 unknown, and term 2 permanent / 37 unknown; 1 accepted freelance job carried the permission marker. Overall and all per-source accounting invariants passed.
  - Dry-run proof: an absent database path remained absent. An existing 25-byte sentinel retained SHA-256 `11aceabfd45ff328287fd5323abaca4359e5fb582351b84d41954886968d74e2` and identical nanosecond mtime before/after all-source `--explain`.
  - The isolated service used 512 MiB, temporary database/logs, no `.env`, disabled Discord/Telegram/Zoho/status sends, and loopback-only `127.0.0.1:18082`. Startup completed in approximately 51.4 seconds with 39 rows saved. `/health` retained all deployment-facing keys, exposed all funnel/source fields, reported Personio `partial_success` with one issue, StepStone complete failure with five issues, JSON-LD zero results, and 10 other healthy sources.
  - Known limitation: Phase 2A intentionally maps no native provider fields, so cautious positive-evidence rules leave most accepted relationships/schedules/terms unknown. Provider audits and structured ATS/source mappings remain deferred to Phase 2B, which was not started.

## Phase 2B — Structured source employment metadata

- Status: Completed on 2026-08-07
- Commit: `feat: map structured employment metadata from job sources` (this Phase 2B commit)
- Tests:
  - Focused source mappings: `python -m pytest tests/v2/test_structured_source_employment.py -q` — 20 passed in 0.30s.
  - Focused mappings plus employment/partial-success regressions — 121 passed in 0.68s.
  - Blocking v2: `python -m pytest tests/v2 -q` — 209 passed in 1.41s.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 209 passed in 1.47s.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30` — 1,086 passed, 104 failed; JUnit comparison found 0 new and 0 missing failing node IDs versus the Phase 0 baseline.
- Peak memory: 347.1 MiB / 512 MiB (67.80%), 11.8 MiB above the 335.3 MiB Phase 2A reference and 82.9 MiB below the 430 MiB target.
- Provider support audit (live diagnostics are from 2026-08-07 and remain non-blocking):

  | Source | Raw structured field and observed values | Normalized mapping | Evidence | Unsupported dimensions/values |
  | --- | --- | --- | --- | --- |
  | Personio | XML `employmentType`: permanent 1,215; intern 77; working_student 43; temporary 15; trainee 5; freelance 4; fixed_term 3; empty 1. XML `schedule`: full-time 1,233; part-time 71; full-or-part-time 58; empty 1. HTML fallback exposes explicit card metadata such as Full-time, Part-time/Vollzeit/Teilzeit, Permanent employee/Festanstellung, Fixed-term, Temporary, Working Student, Internship, and Freelance. | permanent/Permanent employee/Festanstellung → employee + permanent; intern/Internship/Praktikum → internship; working_student/Working Student/Werkstudent → working_student; freelance → freelance; temporary/fixed_term/Fixed-term/Befristet → fixed_term; full-time/Vollzeit → full_time; part-time/Teilzeit → part_time. | Official Personio XML documentation, all 72 configured-board live XML audit, six current HTML fallbacks, saved XML/HTML fixtures. | trainee and full-or-part-time remain unknown; no verified hours, duration, or rate field. |
  | Lever | `categories.commitment`: Full-time 217; Full-Time 101; Permanent Full Time Employee 66; empty 24; Internship 17; Full Time 16; Full-Time (Remote) 10; Fixed Term Contract 8; Full-Time or Part-Time 7; Full-time contract 2; and single Hybrid, Part-Time, Temporary, Student, Werkstudent, Intern, All work types values. | Full-time spelling variants → full_time; Part-Time → part_time; Intern/Internship → internship; Werkstudent → working_student; Fixed Term Contract/Temporary → fixed_term; Permanent Full Time Employee → employee + full_time + permanent. | Official public Postings API field contract, all eight configured-board live audit, saved fixture. | bare/ambiguous contract, Student, Hybrid, Full-Time or Part-Time, All work types, hours, duration, and rate remain unsupported. |
  | Greenhouse | Clearly labelled custom metadata: `Time Type` Full time 870, Part time 37, Full Time 36, Full-time 5, None 2; `Employment Type` Full-time 556, Regular 158, Unlimited Contract 132, Fixed Term 20, Permanent 20 plus lowercase 7, Intern 14, Working Student 10, None 9, Contract 6, Apprentice 1. | Time Type Full/Part variants and Employment Type Full-time → schedule; Unlimited Contract/Permanent → permanent term; Fixed Term → fixed term; Intern/Working Student → relationship. | All 38 configured-board live audit plus saved labelled-metadata fixture. | Regular, Contract, Apprentice, None, arbitrary metadata labels, top-level accessibility value `employment_required`, hours, duration, and rate remain unsupported. |
  | Ashby | `employmentType`: FullTime 3,037; Intern 39; PartTime 20; Contract 14; Temporary 2. | FullTime/PartTime → schedule; Intern → internship; Temporary → fixed term. | Official Ashby public job-posting enum documentation, all 78 configured-board live audit, saved fixture. | Contract is ambiguous; workplaceType and compensation are not employment schedule/relationship/rate evidence. |
  | Workable | `employment_type`: Full-time 8; Other 1 across the three configured boards. | Full-time → full_time. | Live widget audit and saved fixture. | Other and every unobserved value remain unknown; no hours, duration, or rate mapping. |
  | JSON-LD | schema.org `JobPosting.employmentType`, a Text value commonly emitted as a string or array; current configuration has no enabled JSON-LD board. | Explicit Full Time/Part Time → schedule; Intern/Internship/Working Student → relationship; Freelance/Self Employed/Independent Contractor → freelance; Permanent/Fixed Term/Temporary → term. | Current schema.org definition and saved string/array/malformed fixture. | Contract/Contractor, malformed/non-string values, contradictory arrays, hours, duration, and rate remain unsupported. |
  | Arbeitnow | `job_types`: observed Full Time 68; empty 44; Contract 15; repeated explicit permanent/full-time composites; Working student, Intern, Part Time, and mixed seniority labels. | Exact full-time/part-time labels → schedule; exact permanent/full-time-permanent labels → term/schedule; exact Intern/Internship, Working student/Werkstudent, Freelance, Fixed term/Temporary labels → corresponding normalized dimensions. | Live 175-job audit and saved fixture. | Contract, Full or part time, seniority/category labels, hours, duration, and rate remain unsupported. |
  | Remotive | `job_type`: full_time 25; freelance 3; contract 3; part_time 2; empty 1 in the current software-development feed. | full_time/part_time → schedule; freelance → freelance. | Live API audit and saved fixture. | contract and empty remain unknown; no hours, duration, term, or rate mapping. |
  | Himalayas | `employmentType` over the 200-job adapter window: Full Time 170; Contractor 21; Part Time 3; Temporary 3; Intern 3. | Full Time/Part Time → schedule; Contractor → freelance; Temporary → fixed term; Intern → internship. | Official Himalayas API/data dictionary (Contractor explicitly means independent contractor/freelance; Temporary means fixed-term), live audit, saved fixture. | Volunteer/Other and compensation/salary fields remain unsupported; salary is not a freelance rate. |
  | Idealist | `jobType` over 200 remote jobs: FULL_TIME 151; CONTRACT 25; PART_TIME 18; PART_TIME + TEMPORARY 3; FULL_TIME + TEMPORARY 3. | FULL_TIME/PART_TIME → schedule; TEMPORARY → fixed term; CONTRACT → freelance because Idealist's publisher UI defines the category as “Contract / Freelancer.” | Official Idealist publisher help, live Algolia audit, saved fixture. | Volunteer/unknown values, `isFullTime=false`, salary, hours, duration, and rate remain unsupported. |
  | LinkedIn | Guest HTML cards expose title, company, location, URL, and time only. | No structured employment mapping; Phase 2A heuristics retained. | Current adapter contract and live 41-job diagnostic. | Relationship, schedule, term, hours, duration, and rate unsupported. |
  | RemoteOK | Current JSON exposes tags and salary bounds but no dedicated employment field. | No structured employment mapping; Phase 2A heuristics retained. | Live 100-job field-shape audit. | Free-text tags were not promoted; all structured employment dimensions unsupported. |
  | StepStone | Current endpoint still returns HTTP 404 for all five queries; saved minimal fixture contains no employment field. | No mapping; broken endpoint deliberately unchanged. | Existing fixture and live diagnostic. | All structured employment dimensions unsupported. |
- Mapping architecture and precedence:
  - Provider dictionaries stay beside each adapter; one shared `merge_structured_employment_inputs()` drops conflicts independently per dimension, and all adapters pass normalized `EmploymentStructuredInput` values through the Phase 2A classifier.
  - Evidence is compact and provider-qualified (for example `structured:personio:schedule=full_time`, `structured:lever:commitment=full_time`, and `structured:greenhouse:metadata.Time_Type=full_time`). Unknown or malformed values add no structured reason and fall back to normal heuristics.
  - Fixture coverage proves structured schedule plus heuristic term, structured relationship surviving conflicting description text, unsupported values remaining unknown, JSON-LD string/list/malformed handling, and no-support LinkedIn behavior. URL/content hashes and the existing SQLite schema/round-trip remain unchanged.
- Live coverage and behavior:
  - All-default diagnostic: 11,596 raw / 41 accepted / 11,555 rejected; current accepted sources were Greenhouse 16, LinkedIn 14, Personio 5, Ashby 4, and Arbeitnow 2. Counts changed from Phase 2A because live platforms changed.
  - Accepted relationship coverage improved from 1/39 (2.6%) to 4/41 (9.8%); schedule from 7/39 (17.9%) to 21/41 (51.2%); contract term from 2/39 (5.1%) to 8/41 (19.5%). Current distributions are relationship employee 4 / unknown 37; schedule full_time 21 / unknown 20; term permanent 7 / fixed_term 1 / unknown 33.
  - Accepted per-source known relationship/schedule/term: Greenhouse 0/10/3; Ashby 0/4/0; Personio 4/5/5; Arbeitnow 0/1/0; LinkedIn heuristic-only 0/1/0; all other sources had zero accepted jobs in this sample.
  - Raw structured relationship detections were internship 149, working_student 59, employee 1,305, and freelance 18. Student/intern detections came from Personio 122, Greenhouse 24, Ashby 39, Lever 19, and Arbeitnow 4; freelance detections came from Personio 4, Remotive 3, and Idealist 11. The ordered central employment gate produced 82 current employment rejections; earlier location gates still retain terminal ownership when reached first.
  - Source health remained Greenhouse/Ashby/Lever/Workable/Arbeitnow/Remotive/Himalayas/RemoteOK/Idealist/LinkedIn healthy, JSON-LD zero_results, Personio partial_success with one unchanged `pitch` redirect issue, and StepStone complete unknown_error with five unchanged HTTP 404 issues. Malformed listing metadata stayed listing-local and produced no component issue.
  - Routing remained score-only: 8 immediate, 33 digest, 0 diagnostic among the 41 current accepted jobs. No new employment routing distinction exists; the two additional digest jobs versus Phase 2A reflect the changing LinkedIn feed. Structured freelance permission remains informational.
- Verification/runtime notes:
  - Final image: `job-bot:phase2b`, `sha256:62f61855c6e56bf8667727aab0506e49aeda88999e9ec6a513c1b9a57d210aa8`.
  - Required live dry scans ran for Personio, Lever, Greenhouse, Ashby, Workable, JSON-LD, Arbeitnow, Remotive, Himalayas, LinkedIn, Idealist, RemoteOK, StepStone, and all-default `--explain`. No source/query/page/concurrency setting changed.
  - Dry-run proof: a mounted absent database remained absent. A sentinel retained SHA-256 `71d399cc4e75111442ee34638494e9ec436b57ef937a57d16bd492ec4e8263cb` and identical nanosecond mtime `2026-08-07 02:54:36.018522209 +0200` before/after all-source `--explain`.
  - The isolated service used a temporary database/log directory, no `.env`, disabled Discord/Telegram/Zoho/status sends, a 512 MiB limit, and loopback-only `127.0.0.1:18083`. Startup scan completed in approximately 93.6 seconds with 41 rows saved; `/health` reported 11,596 raw, 41 accepted/unseen/saved, 11,555 rejected, 8 immediate, 33 digest, and 0 diagnostic. Peak observed memory was 347.1 MiB. The container and temporary runtime files were removed afterward.
  - No database migration or production dependency was added. Known limitations: JSON-LD has no configured live board; Workable currently proves only Full-time; generic contract values remain intentionally unknown; live coverage depends on changing provider data. Phase 3 was not started.

## Phase 3

- Status: Completed on 2026-08-07
- Commit: `feat: make language filtering requirement-aware` (this Phase 3 commit)
- Tests:
  - Focused language, storage, and presentation matrix: 88 passed in 1.33s.
  - Blocking v2: `python -m pytest tests/v2 -q` — 297 passed in 2.36s.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 297 passed in 2.09s.
  - Historical diagnostic: 1,174 passed, 104 failed, 10 warnings; a node-ID comparison confirmed that the failures exactly match the recorded Phase 0 set, with no new or missing failures.
- Peak memory: 352.2 MiB / 512 MiB, 4.5 MiB above the paired pre-Phase-3 measurement, 5.1 MiB above the Phase 2B reference, and 77.8 MiB below the 430 MiB target.
- Notes:
  - Changed `models/job.py`, `filters/profile.py`, `filters/language.py`, `filters/pipeline.py`, `storage/database.py`, `main.py`, `profile.toml`, `tests/test_filters.py`, `tests/v2/test_language.py`, and `tests/v2/test_language_storage_presentation.py`, plus this Phase 3 checklist/progress entry. No production dependency was added.
  - Added normalized `posting_language`, `german_requirement_status`, `german_requirement_level`, and bounded `language_reasons` fields. The explicit idempotent SQLite migration uses safe `unknown`/`[]` defaults, preserves Phase 2B rows, tolerates malformed reason JSON, and round-trips all new metadata without changing `source_scan_runs`.
  - Added a validated typed `LanguagePolicy` with configuration-driven A1–C2 ordering and backward-compatible unrestricted `accepted_languages` alternatives. The centralized lightweight evaluator keeps deterministic bounded posting-language detection separate from German hiring requirements and compares required CEFR, fluent (minimum B2), business-fluent/professional/verhandlungssicher (minimum C1), and native (never implied by CEFR) evidence.
  - Required, optional, negated, irrelevant course/training, ambiguous, multiple-clause, and `or`/`and` alternative contexts are handled conservatively. Optional context wins over severity; only `incompatible` rejects; explicit unmodeled English proficiency alternatives pass as `unknown`. The evaluator remains at the existing language gate after employment/role/stack and preserves the stable `language` rejection code.
  - CLI and `--explain` add at most one compact language line only when useful. Ordinary English/unspecified output remains quiet; Discord, Telegram, digest formatting, match scoring, thresholds, tiers, company caps, source behavior, and scheduling/concurrency are unchanged.
  - Bounded pre-change live diagnostic: 11,566 raw / 37 accepted / 30 terminal language rejections / 8 immediate / 29 digest / 0 diagnostic. Rejections classified as German prose only 13, B2 0, C1 0, C2 0, fluent 2, business-fluent/professional/verhandlungssicher 0, native/Muttersprache 2, other detected posting language 12, and ambiguous German mention 1.
  - Same-sample Phase 3 comparison: the prior gate would reject 29 and accept 37; Phase 3 rejected 2 and accepted 58, with 22 newly accepted and one newly rejected after stronger explicit native evidence. Eleven German-prose-only and eleven other-language-prose jobs were newly accepted. Thirteen ambiguous evaluations passed the language gate as `unknown`; terminal required evidence included one fluent and one native incompatibility. No terminal B2/C1/C2 or business-fluent live example appeared, so those policies are proven by fixtures. Routing moved from 8 immediate / 29 digest / 0 diagnostic to 8 / 50 / 0 solely through changed eligibility; score/tier invariance is covered by regression tests.
  - Required Arbeitnow, Personio, LinkedIn, and all-default `--explain` dry scans passed. An absent database remained absent; the sentinel database retained SHA-256 `885ea9faad2abe6684c3bcf793f81ee660a195be3eb66e8cf334991ed1489827` and identical nanosecond mtime `2026-08-07 11:56:46.813940126 +0200` after every dry scan.
  - Final image: `job-bot:phase3`, `sha256:647945954b9796ef69f9b53e841edfbdfd683ea443a509b991eaee1264d3182f`, 374,425,048 bytes. The isolated service used a temporary database/log tree, no `.env`, disabled Discord/Telegram/Zoho/status sends, a 512 MiB limit, and loopback-only `127.0.0.1:18084`; `/health` remained responsive.
  - Paired pre-change startup was approximately 92.4 seconds at 347.7 MiB with 11,565 raw / 37 accepted / 8 immediate / 29 digest / 0 diagnostic. Final startup was approximately 91.6 seconds at 352.2 MiB with 11,575 raw / 58 accepted / 8 immediate / 50 digest / 0 diagnostic: approximately 0.9% faster and only 4.5 MiB higher, so neither investigation threshold was reached.
  - Final persistence contained 58 jobs with all four columns populated safely: posting language `de` 11 / `en` 34 / `other` 11 / `unknown` 2, and German requirement status `optional` 1 / `unspecified` 57. Employment data persisted unchanged. Personio remained `partial_success` with one known redirect issue; StepStone remained isolated `unknown_error` with five HTTP 404 issues; JSON-LD remained `zero_results`; other configured sources were healthy.
  - Known limitations: extraction is intentionally regex/rule based; ambiguous evidence passes conservatively; `accepted_languages` does not model explicit per-language proficiency; current live feeds are volatile; no current terminal live B2/C1/C2/business-fluent example was available. Phase 4 was not started.

## Phase 4A — Notification tier and delivery-state foundation

- Status: Completed on 2026-08-07
- Commit: `feat: make job notification delivery idempotent` (this Phase 4A commit)
- Tests:
  - Focused delivery/observability/employment regression gate: `python -m pytest tests/v2/test_notification_delivery.py tests/v2/test_observability.py tests/v2/test_employment_storage_presentation.py -q` — 91 passed in 1.69s.
  - Blocking v2: `python -m pytest tests/v2 -q` — 334 passed in 3.13s.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 334 passed in 3.10s.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30` — 1,211 passed, 104 failed, 9 warnings in 11.53s. A separate sorted node-ID comparison found exactly the recorded 104 failures, with no new or missing failing IDs.
- Peak memory: 343.5 MiB / 512 MiB (67.08%), 8.7 MiB below the 352.2 MiB Phase 3 reference and 86.5 MiB below the 430 MiB target.
- Notes:
  - Files changed: `models/job.py`, `models/scan.py`, `storage/database.py`, `notifiers/base.py`, `notifiers/delivery.py`, `notifiers/discord_notifier.py`, `notifiers/telegram_notifier.py`, `main.py`, `health.py`, `tests/v2/test_notification_delivery.py`, `tests/v2/test_observability.py`, `tests/v2/test_employment_storage_presentation.py`, and this Phase 4A checklist/progress entry.
  - Added the valid `none`/`explore`/`digest`/`immediate` model while leaving production assignment at the unchanged 70 immediate / 45 digest / below-45 none boundaries. Live and isolated scans reported `explore=0`; scoring, the company cap, employment compatibility, and source schedules were not changed.
  - Added the idempotent `job_delivery_receipts(job_id TEXT NOT NULL, delivery_kind TEXT NOT NULL, destination TEXT NOT NULL, delivered_at TEXT NOT NULL, PRIMARY KEY (job_id, delivery_kind, destination))` migration plus pending/receipt indexes. A representative Phase 3 database initialized twice safely; the migration preserved `jobs.notified`, rewrote no historical job, fabricated no receipt, and left `source_scan_runs` unchanged.
  - Delivery kinds are `immediate`, `digest`, and schema-ready `explore`; destinations are `discord_general`, `discord_ngo`, and `telegram`. One shared resolver sends general jobs to Discord general, NGO jobs to Discord NGO when configured, and NGO jobs to general as fallback; a receipt at either Discord destination satisfies the one Discord obligation across later configuration changes. Telegram remains independent and digest remains Discord-general only.
  - Discord and Telegram batch methods now return exact successful job/destination pairs. Per-job exceptions and HTTP statuses at least 400 omit only failed jobs; sibling successes continue and are recorded in one transactional `INSERT OR IGNORE` batch. Unconfigured channels create no synthetic success.
  - `jobs.notified=1` now has the documented conservative meaning `legacy_suppressed`; it is not evidence of historical provider delivery. New immediate/digest paths never call `mark_notified()` and keep new rows at `notified=0`; destination receipts are authoritative.
  - Immediate delivery now runs from durable pending state after every production scan, including a scan saving zero rows. Focused and isolated runtime checks proved Discord-success/Telegram-failure and inverse retries, exact sibling receipts, configuration-change suppression, and retrying only the missing destination.
  - Immediate/digest queries use `fetched_at` within 14 days, `notified=0`, receipt absence, a 15-item bound, and exact ordering by score, posted/fetched recency, fetched recency, then ID. The six-hour value remains schedule cadence only. Tests proved seven-day-old digest work is retained, 15+ overflow carries to later runs, 15-day stale work is excluded without a receipt, and two isolated runtime digest runs each delivered the next 15 jobs with 20 still pending.
  - The grouped digest builder retains exact rendered membership under the Discord description limit. Successful sends receipt only included jobs; selected-but-not-rendered overflow remains pending, and a failed grouped payload records none.
  - Added `explore` additively to per-source/current routing counts, persisted JSON restoration, `/health`, `--stats`, and daily status while retaining `immediate`, `digest`, and `diagnostic`; saved `none` remains diagnostic. The final isolated health summary was 11,586 raw / 58 accepted-unseen-saved / 11,528 rejected / 8 immediate / 50 digest / 0 explore / 0 diagnostic.
  - Dry scans passed for Arbeitnow (175 raw / 1 accepted) and all-source `--explain` (11,585 raw / 58 accepted). An absent database remained absent; a 25-byte sentinel retained SHA-256 `61126de1b795b976f3ac878f48e88fa77a87d7308ba57c7642b9e1068403a496` and identical nanosecond mtime before/after dry-run.
  - Final image: `job-bot:phase4a`, `sha256:19cec7e70350d72a09b0dc552f19e01c87df28cc25c97555355ce3d9a1a702c3`, 374,776,815 bytes. The isolated service used temporary DB/log mounts, no `.env`, disabled real notifier/status/Zoho sends, a 512 MiB limit, and loopback-only `127.0.0.1:18085`; startup completed in approximately 95.8 seconds, about 4.6% above the 91.6-second Phase 3 reference and below the 10% investigation threshold. Temporary runtime data/container were removed.
  - Runtime inspection confirmed the exact receipt columns, zero fabricated receipts, all 58 new rows retained `notified=0`, 8 immediate mocked deliveries receipted on the first run and 0 selected on retry, two 15-job digest batches carried over correctly, all 58 language records remained populated, and 23 rows retained known employment metadata. Personio remained `partial_success`, StepStone remained isolated `unknown_error`, and JSON-LD remained `zero_results`.
  - Weekly NGO jobs remain outside one-time receipt semantics and repeat in focused regression coverage. Known limitation: provider HTTP and SQLite receipt commits cannot be atomic, so a crash after external acceptance but before receipt commit can produce an at-least-once duplicate on retry. No broker or exactly-once claim was added. Phase 4B and Phase 5 remain not started.

## Phase 4B — Recall policy, explore digest, and company-cap tuning

- Status: Completed on 2026-08-07
- Commit: `feat: add bounded explore routing and recall tuning` (this Phase 4B commit)
- Tests:
  - Focused policy/delivery/observability/presentation gate: `python -m pytest tests/v2/test_notification_policy.py tests/v2/test_notification_delivery.py tests/v2/test_observability.py tests/v2/test_employment_storage_presentation.py -q` — 151 passed in 4.32s.
  - Blocking v2: `python -m pytest tests/v2 -q` — 394 passed in 9.49s.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 394 passed in 7.74s.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30` — 1,271 passed, 104 failed, 10 warnings in 21.49s. A sorted node-ID comparison found exactly the recorded 104 failures, with no new or missing failures.
- Peak memory: 344.2 MiB / 512 MiB (67.23%), 0.7 MiB above the documented 343.5 MiB Phase 4A reference, 6.4 MiB below the paired current-network Phase 4A run, and 85.8 MiB below the 430 MiB target.
- Read-only simulation architecture and proof:
  - Added `python main.py --simulate-notifications`. It fetches each configured source once through the normal typed source-outcome path, runs the one global production hard-gate/employment/language/scoring pipeline with only final company-cap application disabled, then evaluates policy scenarios over the retained pre-cap candidates.
  - The simulator bypasses database initialization, deduplication/persistence, source metrics, delivery, ATS discovery, health publication, and Zoho/mail work. Automated forbidden-call tests passed. Runtime runs proved an absent DB remained absent; an existing DB and Zoho sentinel retained identical SHA-256 and mtime; no discovery file was created; no posting description was printed or retained in the report.
  - Both the pre-rollout simulator and the finalized-image repeat produced the same aggregate evidence below, despite being separate live runs.
- Live pre-cap evidence from the finalized bounded simulation:
  - Hard-eligible pre-cap total: 79. Score bands: 70–100 = 16; 45–69 = 61; 30–44 = 2; 15–29 = 0; 0–14 = 0.
  - Sources: Arbeitnow 1; Ashby 16; Greenhouse 20; Lever 1; LinkedIn 33; Personio 8.
  - Relationships: employee 7; freelance 1; unknown 71. Schedules: full-time 36; unknown 43; no part-time candidate. Contract employee 0; fixed-term 1; contract-or-fixed-term union 1; freelance 1; permission-required freelance 1.
  - Company concentration before cap: 46 companies; Clēra 14, adesso SE 5, Flix/GetYourGuide/Nebius/Speechify/Sunhat 3 each, Autohaus Royal/Beroe/Cresta/Doctolib/Mogic/Parloa 2 each, and 33 companies with 1. Current score-only cap 2 excluded 20 candidates.
- Complete A/B/C and cap 2–5 selected aggregates (visibility is `part-time / contract-or-fixed-term / freelance / permission-required`; score bands are `70–100 / 45–69 / 30–44 / 15–29 / 0–14`):

  | Scenario | Immediate | Digest | Explore | None | Cap exclusions | Visibility | Selected score bands | Lowest selected I/D/E | Selected company concentration |
  | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
  | A current, score-only cap 2 | 10 | 49 | 0 | 0 | 20 | 0 / 1 / 0 / 0 | 10 / 49 / 0 / 0 / 0 | 70 / 45 / — | 13 companies ×2; 33 ×1; max 2/59 (3.4%) |
  | B 70/30/15, diversity cap 2 | 10 | 49 | 0 | 0 | 20 | 0 / 1 / 0 / 0 | 10 / 49 / 0 / 0 / 0 | 70 / 45 / — | 13 ×2; 33 ×1; max 2/59 (3.4%) |
  | B 70/30/15, diversity cap 3 | 11 | 55 | 0 | 0 | 13 | 0 / 1 / 0 / 0 | 11 / 53 / 2 / 0 / 0 | 70 / 41 / — | 7 ×3; 6 ×2; 33 ×1; max 3/66 (4.5%) |
  | B 70/30/15, diversity cap 4 | 12 | 56 | 0 | 0 | 11 | 0 / 1 / 0 / 0 | 12 / 54 / 2 / 0 / 0 | 70 / 41 / — | 2 ×4; 5 ×3; 6 ×2; 33 ×1; max 4/68 (5.9%) |
  | B 70/30/15, diversity cap 5 | 13 | 57 | 0 | 0 | 9 | 0 / 1 / 0 / 0 | 13 / 55 / 2 / 0 / 0 | 70 / 41 / — | 2 ×5; 5 ×3; 6 ×2; 33 ×1; max 5/70 (7.1%) |
  | C 70/45/30, diversity cap 2 | 10 | 49 | 0 | 0 | 20 | 0 / 1 / 0 / 0 | 10 / 49 / 0 / 0 / 0 | 70 / 45 / — | 13 ×2; 33 ×1; max 2/59 (3.4%) |
  | C 70/45/30, diversity cap 3 | 11 | 53 | 2 | 0 | 13 | 0 / 1 / 0 / 0 | 11 / 53 / 2 / 0 / 0 | 70 / 45 / 41 | 7 ×3; 6 ×2; 33 ×1; max 3/66 (4.5%) |
  | C 70/45/30, diversity cap 4 | 12 | 54 | 2 | 0 | 11 | 0 / 1 / 0 / 0 | 12 / 54 / 2 / 0 / 0 | 70 / 45 / 41 | 2 ×4; 5 ×3; 6 ×2; 33 ×1; max 4/68 (5.9%) |
  | C 70/45/30, diversity cap 5 | 13 | 55 | 2 | 0 | 9 | 0 / 1 / 0 / 0 | 13 / 55 / 2 / 0 / 0 | 70 / 45 / 41 | 2 ×5; 5 ×3; 6 ×2; 33 ×1; max 5/70 (7.1%) |

  - All live diversity-versus-score-only comparisons had identical selected IDs and zero score trade-off because no competing same-tier company group contained a useful unrepresented employment bucket. Every comparison preserved tier counts. Focused fixtures separately prove the documented same-tier diversity replacement and quantify its score delta without a category multiplier.
- Evidence-selected production policy:
  - Selected `immediate_score=70`, `digest_score=45`, `explore_score=30`, `daily_explore_enabled=true`, and `max_jobs_per_company=2`; retained `explore_hour_utc=17`, immediate/digest/explore limits 15/15/10, `pending_max_age_days=14`, and `freelance_permission_max_tier="digest"`.
  - The 30/15 aggressive thresholds were not justified: the live sample contained no 15–29 or 0–14 candidates, so it provided no relevance evidence for the proposed explore band, while its two 30–44 candidates would be promoted into the regular digest. Conservative 45/30 keeps those candidates in explore when a company slot is available.
  - Cap 3 was the smallest cap with more selected jobs (+7 versus A), but it added no part-time/freelance/contract visibility and raised seven employers to three selected jobs. Caps 4/5 increased concentration further without employment-diversity benefit. Evidence was therefore inconclusive for a cap increase, so the approved cap-2 fallback was retained.
  - Final C cap 2 versus A on the same sample: immediate ±0, digest ±0, explore 0, none ±0, company-cap exclusions ±0, company concentration unchanged, employment visibility unchanged, and lowest selected scores 70 immediate / 45 digest. The one permission-required freelance candidate remained hard-eligible but was cap-excluded; fixtures prove an otherwise-immediate marked freelance job routes to digest without rejection.
- Implementation notes:
  - Added one frozen validated `NotificationPolicy` loaded from `profile.toml`, one exact central tier function, and one routing-only freelance ceiling. Scoring arithmetic, hard eligibility, and employment compatibility are unchanged.
  - Replaced the hardcoded cap with deterministic tier-first selection ranked by score, posted/fetched recency, fetched recency, then ID. Employment buckets are mutually exclusive; diversity is considered only within the current tier; the one overall cap is never multiplied; every exclusion receives one `company_cap` terminal result. Overall/per-source accounting remained valid.
  - Added a 17:00 UTC APScheduler explore job only when enabled. Explore is Discord-general only, uses `delivery_kind=explore` receipts, never uses Telegram or immediate delivery, selects at most 10, carries overflow, excludes stale work, records exact included IDs only after success, and cannot overlap the mutually exclusive digest tier.
  - Immediate/digest limits and pending age now come only from the typed policy. Storage accepts those values rather than owning duplicate defaults. Immediate destination semantics and missing-receipt retry behavior remain unchanged.
  - Digest/explore payloads group each included job exactly once under standard, part-time/contract/fixed-term, or freelance sections. Output remains within the Discord description limit and retains title, company, employment metadata, score, URL, and permission warning without descriptions.
  - No database schema migration or production dependency was added. `source_scan_runs` remained unchanged; explore/diagnostic health, stats, restoration, and daily status behavior passed regression coverage.
- Runtime verification:
  - Final image: `job-bot:phase4b`, `sha256:5ee01ce0247c18c3a833a0147058b0dc69e4164648675a2249813742f0075f80`, 374,949,818 bytes.
  - Dry scans passed: Arbeitnow 175 raw / 1 accepted; Personio 1,397 / 7 with the known one-board partial success; LinkedIn 40 / 30; all-source explain 11,600 / 59. Across all four, DB and Zoho sentinel SHA-256/mtime stayed identical and the ATS discovery mount remained empty.
  - The isolated 512 MiB Phase 4B service used temporary DB/log mounts, no `.env`, disabled real Discord/Telegram/status/Zoho sends, disabled ATS writes, and loopback-only `127.0.0.1:18086`. `/health` completed with 11,604 raw / 59 accepted-unseen-saved / 11,545 rejected / 10 immediate / 49 digest / 0 explore / 0 diagnostic; Personio remained partial, StepStone blocked, and JSON-LD zero-results.
  - Startup took 206 seconds during repeated live ATS disconnect/retry delays. A paired unchanged Phase 4A image under the same current conditions took 191 seconds, making Phase 4B +7.9%, below the 10% investigation threshold. Phase 4B peak memory was 344.2 MiB versus paired Phase 4A 350.6 MiB.
  - Seeded fake-transport runtime proof: two immediate jobs produced four Discord/Telegram receipts and zero work on retry; 17 digest jobs delivered 15 then 2; 12 explore jobs delivered 10 then 2; total receipts were 33 across immediate/digest/explore; a 15-day stale explore job got no receipt; no explore Telegram receipt existed. A receipted NGO job remained present in two consecutive weekly queries (1/1), preserving repeated weekly behavior.
  - Restart proof restored the persisted 59-job health summary within four seconds, before the next startup scan completed, and registered the explore cron at 17:00 UTC.
  - Temporary containers, databases, receipts, logs, live simulation output, and runtime files were removed after verification. The narrow provider-HTTP/receipt-commit at-least-once crash window remains unchanged. Live source volume and timing remain volatile; this sample had no selected explore, part-time, or freelance job, so later production evaluation should revisit recall only with new evidence.
  - Phase 5 was not started.

## Phase 5A — Grouped Scheduler, Locking, Health, and Bounded Source HTTP

- Status: Completed
- Commit: `feat: group and bound production source scans` (this Phase 5A commit)
- Tests:
  - Focused Phase 5A: 33 passed, with one third-party `audioop` deprecation warning.
  - Blocking v2 and CI-equivalent gates: 427 passed for `python -m pytest tests/v2 -q`; 427 passed for `python -m pytest -q --timeout=30`.
  - Historical diagnostic: 1,304 passed / exactly the same 104 known failures / 10 warnings; no new or missing failing node IDs.
  - Final-image runtime contract probes: seven passed for scheduled FIFO/no overlap, cross-group URL/receipt deduplication, ATS append idempotency, dry-run/simulation immutability, and deterministic readiness/restart.
- Peak memory: 330 MiB / 512 MiB during the selected staggered production-like Group A/B refresh, below the 430 MiB hard target and the 344.2 MiB Phase 4B reference.
- Notes:
  - Added the authoritative application source catalog. Group A is exactly Greenhouse, Ashby, Personio, Lever, Workable, and JSON-LD at 60 minutes. Group B is exactly Arbeitnow, Remotive, Himalayas, RemoteOK, Idealist, and LinkedIn at 120 minutes. StepStone and every impact/optional adapter remain manually addressable; Group C is absent.
  - Split source limits into adapter/component/HTTP controls. The measured production defaults are `MAX_CONCURRENT_SOURCE_ADAPTERS=2`, `MAX_CONCURRENT_SOURCE_COMPONENTS=3`, and `MAX_CONCURRENT_HTTP_REQUESTS=4`; legacy `MAX_CONCURRENT_SOURCES` is only an adapter fallback.
  - Added a scan-local source HTTP budget with configured/current/peak counters and cancellation-safe release. GET/POST retries release before backoff. Idealist now uses the shared POST transport. The scheduled-source audit found no remaining direct-network bypass and requires a non-empty reviewed catalog rationale for any future exception; no Phase 5A exception exists.
  - Replaced per-item semaphore queues with fixed-size local worker pools. Every fan-out boundary owns its local workers; nested Personio HTML-detail work therefore cannot recursively acquire an outer component permit. The shared HTTP budget remains the absolute network-attempt ceiling.
  - The paired experiment used the unchanged `job-bot:phase4b` image for A and interim image `sha256:2ee1057b0eed5da659cb5ef9dc70bd2fe02bae130e27b56c4c7ab81d167f6963` for B/C, in A/B/C then C/B/A order for each group:

    | Scope/config | Paired durations | Median / slow | Peak memory | Raw | Pre-cap / final | HTTP peak and events |
    | --- | --- | --- | --- | --- | --- | --- |
    | Group A / A | 233.0s, 261.3s | 247.1s / 261.3s | 337, 339 MiB | 10,939–10,940 | 36 / 24 | no unified ceiling; Personio retained its known one-component partial state |
    | Group A / B | 224.4s, 263.4s | 243.9s / 263.4s | 316, 317 MiB | 10,900–10,941 | 36 / 24 | 4/4; 244–247 attempts, 6–9 retries, 0 rate limits |
    | Group A / C | 259.2s, 272.7s | 266.0s / 272.7s | 330, 339 MiB | 10,941 | 36 / 24 | 6/6; 248 attempts, 10 retries, 0 rate limits |
    | Group B / A | 13.1s, 14.6s | 13.8s / 14.6s | 126, 125 MiB | 387 | 33 / 30 | no unified ceiling; all sources healthy |
    | Group B / B | 15.1s, 16.1s | 15.6s / 16.1s | 125, 124 MiB | 387 | 33 / 30 | 4/4; 26 attempts, 0 retries/rate limits; all sources healthy |
    | Group B / C | 20.6s, 15.7s | 18.2s / 20.6s | 125, 126 MiB | 387 | 33 / 30 | 6/6; 26 attempts, 0 retries/rate limits; all sources healthy |

  - Selected B because Group A was approximately 1.3% faster than paired A and Group B approximately 12.7% slower, both within the 20% bound, with lower Group A memory and unchanged useful coverage. One B Group A pass lost 41 raw jobs (0.4%) through isolated Greenhouse/Ashby component failures; the paired passes and final runtime recovered, with no repeated failure, rate limit, hard-eligible loss, or >5% source drop.
  - Selected startup offsets are Group A +1 minute and Group B +6 minutes. The Group B offset uses the normal 243.9-second Group A duration rounded to four minutes plus approximately two minutes; the 263.4-second slow pass remains coordinator-protected. Jobs use stable IDs `source_group_a`/`source_group_b`, `max_instances=1`, coalescing, and 300-second misfire grace.
  - Added one production coordinator around fetch, ATS discovery, filtering/cap, dedup/save, metrics/health, and pending immediate delivery. Scheduled groups wait FIFO; manual Discord/Telegram scans never queue and receive only the active scope/start time. Success, exception, and cancellation clear coordinator state.
  - Core readiness now transitions false → true after SQLite/restoration, health binding, scheduler registration/start, and supervised local tasks, without awaiting Discord, Telegram, Zoho, or source connectivity; shutdown returns it to false. Final isolated core readiness took approximately 3.0 seconds.
  - Added the idempotent non-null `scan_scope` migration with `legacy_all` default and scope/completion indexes. New scopes persist as `group_a`, `group_b`, or `manual_all`. Restored scan/group completion uses scan-level completion while per-source operational timestamps retain each source attempt time.
  - Health preserves every legacy field and adds readiness, latest scope, bounded HTTP counters, Group A/B completion freshness, and the nearest group trigger. Restart restored the latest Group B summary plus authoritative Group A/B per-source health before a new scan; daily status labels scope and group freshness.
  - Final isolated 512 MiB run used a temporary database/log directory, no project `.env`, disabled real Discord/Telegram/Zoho sends, no Group C, and loopback health. Group A triggered at +60 seconds and completed its full lifecycle in approximately 236 seconds with 10,938 raw / 36 pre-cap / 24 final/saved, HTTP 4/4, 244 attempts, six retries, and no rate limit. Group B triggered at +360 seconds and completed in approximately 15 seconds with 386 raw / 32 pre-cap / 29 final/saved, HTTP 4/4, 26 attempts, and no retry/rate limit. Both completed by approximately 378 seconds; readiness remained true throughout.
  - LinkedIn remained healthy and useful in the final Group B refresh with 38 raw / 29 accepted. Personio retained the known bounded Pitch redirect partial issue. StepStone remains manual-only and its final diagnostic returned 0 raw with five HTTP 403 component issues; it was not repaired.
  - Across the final Group A/B windows all 53 selected jobs were newly saved, no cross-group URL/content dedup loss occurred, and no normalized company exceeded two selections. Fixture/runtime probes preserve receipt idempotency, scan-local cap accounting, scope-local ATS discovery with append deduplication, and dry-run/simulation write-freedom.
  - Final image: `job-bot:phase5a`, `sha256:3cc8d5e9bbabfb949b474e9d2797e19b48909a4e426a686ae034d5568a4c8ca4`, 375,342,313 bytes. No Playwright/Chromium dependency, production import, process, or dead disable setting was introduced. All temporary experiment/runtime containers, databases, logs, and aggregate runner files were removed.
  - Live provider volumes/timing remain volatile; the known Personio redirect remains isolated and StepStone remains blocked/manual-only. Existing deployments that set only deprecated `MAX_CONCURRENT_SOURCES` should add the three measured explicit controls to use the selected defaults. Phase 5B and Phase 6 were not started.

## Phase 5B — Impact-Source Admission

- Status: Completed on 2026-08-11
- Commit: `docs: record Phase 5B impact-source admission audit` (this Phase 5B commit)
- Tests:
  - Focused Phase 5A scheduling/runtime admission foundation: `python -m pytest tests/v2/test_phase5a_scheduling.py tests/v2/test_phase5a_runtime.py -q` — 33 passed, one third-party `audioop` deprecation warning.
  - Blocking v2: `python -m pytest tests/v2 -q` — 427 passed, one warning.
  - CI-equivalent: `python -m pytest -q --timeout=30` — 427 passed, one warning.
  - Historical diagnostic: `python -m pytest tests -q --timeout=30 --tb=no` — 1,304 passed, exactly the same 104 known failures, and 11 warnings; no production code changed and the failing node IDs match the Phase 5A baseline.
  - Final-image catalog probe: passed; all six candidates resolve manually, all six remain `manual_only`, none appears in the scheduled union, and `source_group_c` is absent.
- Peak memory:
  - Candidate diagnostics ran one source per Python 3.11 process inside the verified 512 MiB Phase 5A image. Maximum process RSS ranged from 62.8 to 72.5 MiB; 80,000 Hours was the highest at 72.5 MiB, well below the 430 MiB hard target.
  - The final Phase 5B image's isolated core service used 54.5 MiB / 512 MiB before the first scheduled refresh, reported `status=ok` and `ready=true`, and registered 12 sources across exactly two groups. No Group C runtime scan was required because no candidate qualified.
- Live admission audit:

  | Source | Status | Raw | Hard-eligible pre-cap | Final | Duration | Issues | Source HTTP limit / peak | Attempts / retries / rate limits | Max RSS | Decision |
  | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | ---: | --- |
  | GoodJobs | `zero_results` but not trustworthy | 0 | 0 | 0 | 1.3s | parser drift; typed issue count 0 | 4 / 1 | 1 / 0 / 0 | 71.4 MiB | manual-only |
  | ReliefWeb | `healthy` | 21 | 0 | 0 | 16.9s | 0 typed issues; one recovered network retry | 4 / 3 | 4 / 1 / 0 | 69.0 MiB | manual-only |
  | Devex | `healthy` | 20 | 0 | 0 | 1.4s | 0 | 4 / 1 | 2 / 0 / 0 | 62.8 MiB | manual-only |
  | EuroBrussels | `healthy` | 28 | 0 | 0 | 2.2s | 0 | 4 / 1 | 2 / 0 / 0 | 70.4 MiB | manual-only |
  | 80,000 Hours | `healthy` | 42 | 0 | 0 | 2.8s | 0 | 4 / 1 | 3 / 0 / 0 | 72.5 MiB | manual-only |
  | Tech Jobs for Good | `blocked` | 0 | 0 | 0 | 0.5s | one `blocked` issue: HTTP 403 | 4 / 1 | 2 / 0 / 0 | 62.9 MiB | manual-only |

- Admission decisions and evidence:
  - **GoodJobs — manual-only.** The current server response was 753,347 characters but the adapter normalized no jobs. A successful HTTP response is therefore still reported as `zero_results` even though the evidence indicates parser drift, so normalization, current field quality, useful yield, and trustworthy zero-result outcome semantics are not demonstrated. Repair was intentionally not attempted.
  - **ReliefWeb — manual-only.** All 21 raw jobs had valid title/company/URL, 19 had a non-placeholder location, and 20 content hashes were unique, but all 21 were rejected because Germany/Berlin eligibility was unknown. Relationship, schedule, and contract-term metadata were entirely unknown. Its three-feed fan-out stayed within the shared ceiling and recovered one transient request, but the adapter does not record per-feed success/failure through the component outcome collector, so mixed-feed partial-success semantics remain insufficient for admission.
  - **Devex — manual-only.** All 20 raw jobs normalized title/company/location/URL, with 19 unique content hashes. Sixteen failed location eligibility; four had explicit worldwide location evidence but all four failed the role gate, leaving no plausible useful tech yield. Native employment dimensions were entirely unknown. Its two sequential pages are budgeted, but a later-page failure after usable results would not be reported as partial success.
  - **EuroBrussels — manual-only.** All 28 raw jobs normalized required fields and unique content, but all 28 had unknown Germany/Berlin eligibility and were location-rejected. Native employment dimensions were entirely unknown and useful yield was zero. Both sequential page requests were budgeted, but mixed-page failure semantics are not instrumented as partial success.
  - **80,000 Hours — manual-only.** All 42 source-prefiltered jobs normalized required fields and unique content. Thirty-five failed location eligibility; seven had explicit acceptable location evidence (six worldwide and one EU) but all seven failed the role gate. Native employment dimensions were entirely unknown and useful yield was zero. The three sequential Algolia pages were budgeted, but mixed-page failure semantics are not instrumented as partial success. The existing broad source-side remote default was not used to weaken the hard eligibility gate.
  - **Tech Jobs for Good — manual-only.** Both public URL attempts remained blocked by Cloudflare HTTP 403. The typed outcome correctly reported `blocked`, but unattended public access, normalized fields, useful yield, and stable production behavior cannot be demonstrated without changing the prohibited access strategy; no bypass or repair was attempted.
- Network, concurrency, and fixture review:
  - All six adapters use the shared `BaseSource._get()`/`_post()` transport; a static direct-network audit found no `httpx.AsyncClient`, `aiohttp.ClientSession`, `requests`, `urllib.urlopen`, or URL-fed `feedparser` bypass in their modules. No reviewed HTTP-budget exception is needed.
  - ReliefWeb has exactly three concurrent feed tasks, matching the component setting and remaining under the scan-wide ceiling; the other candidates use one request or bounded sequential page attempts. None has nested/recursive component limiting, browser runtime, Playwright/Chromium, private authentication, or unbounded task creation. 80,000 Hours uses only the public browser-exposed Algolia search key.
  - Existing mocked historical adapter fixtures remain available, but no candidate received production admission or a justified localized correction. In accordance with the conditional Phase 5B task, no new fixture or adapter test was added solely to make a manual-only source appear admissible.
- Verification/runtime notes:
  - Final image: `job-bot:phase5b`, `sha256:68b5c6f8c28fe008874b71c8f6d84cc1ab8e948b9e328746078e7dd90954c4f2`, 375,344,950 bytes.
  - A final-image GoodJobs dry run remained read-only and reproduced 0 raw / 0 accepted with the same 753,347-character parser-drift response, HTTP peak 1/4, no retry, and no rate limit.
  - The isolated service used temporary database/log mounts, no project `.env`, disabled real Discord/Telegram/status/Zoho sends and ATS discovery, a 512 MiB limit, and loopback-only `127.0.0.1:18087`. `/health` became ready before source refresh, with the next Group A trigger approximately 51 seconds away; temporary runtime data and the container were removed after validation.
  - No production source, catalog, scheduler, configuration, database schema, dependency, fixture, or test file changed. Group C membership remains empty and its scheduler job remains absent. Every candidate remains manually runnable for bounded diagnostics.
  - Known limitations: live feeds and provider latency remain volatile; no seven-day production yield comparison exists for manual-only candidates; GoodJobs parser drift, candidate component-outcome gaps, Tech Jobs for Good access, and StepStone remain deliberately unrepaired. Phase 6 was not started.

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
