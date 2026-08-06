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

- [ ] Create a feature branch or worktree, for example:
  - `feat/coverage-and-employment-types`
- [ ] Record Python and dependency versions.
- [ ] Run:
  - `python -m pytest tests/v2 -q`
  - `python -m pytest -q --timeout=30`
- [ ] Record any pre-existing failures without changing unrelated behavior.
- [ ] Run representative dry scans:
  - `python main.py --dry-run --source arbeitnow`
  - `python main.py --dry-run --source remotive`
  - `python main.py --dry-run --source himalayas`
  - `python main.py --dry-run --source linkedin`
  - `python main.py --dry-run --explain`
- [ ] Build the production image.
- [ ] Run the production Compose service with current settings.
- [ ] Record baseline container memory with `docker stats --no-stream`.
- [ ] Record baseline `/health` output.
- [ ] Document whether `README.md` and `.github/workflows/deploy.yml` disagree about which tests form the CI gate.

## Definition of done

- Baseline commands and results are recorded in the progress log.
- No product behavior has changed.
- Pre-existing test failures, if any, are clearly separated from new failures.

---

# Phase 1 — Pipeline observability and source health

## Objective

Make it obvious whether a platform returned no jobs, failed, was blocked, or returned jobs that were later rejected.

## Design

Introduce lightweight per-source scan metrics. Avoid a monitoring framework.

Minimum metrics per source and scan:

- source name
- started_at / finished_at
- duration_ms
- status: `healthy`, `zero_results`, `rate_limited`, `blocked`, `parse_error`, `network_error`, `unknown_error`
- raw count
- rejected by:
  - duplicate_in_memory
  - company_blocklist
  - location
  - role
  - stack
  - language
  - seniority
  - salary
  - recency
  - minimum_score
  - company_cap
- accepted count
- unseen count
- saved count
- immediate count
- digest count
- explore count
- no-notification count
- short error message, with no secrets or email content

## Tasks

- [ ] Define typed result objects, preferably dataclasses, for:
  - source fetch outcome
  - filter outcome/rejection reason
  - scan summary
- [ ] Replace the current “exception becomes empty list” ambiguity with a result that preserves the error category while still isolating the source.
- [ ] Keep a compatibility path for existing tests and source classes where practical.
- [ ] Refactor filtering so each rejection has a stable machine-readable reason code and human-readable explanation.
- [ ] Preserve current `--verbose` output.
- [ ] Add a SQLite `source_scan_runs` table or an equally lightweight persisted structure.
- [ ] Keep only a bounded history, such as 14–30 days, through periodic cleanup.
- [ ] Extend `/health` with:
  - latest overall scan summary
  - latest status per source
  - last successful scan time per source
  - counts at every pipeline stage
- [ ] Extend `--stats` with a compact source-health section.
- [ ] Extend the daily Discord status with source failures and funnel counts, but do not flood the channel.
- [ ] Add tests for zero-results vs failure, rejection reason accounting, persistence, health output, and cleanup.
- [ ] Ensure exception text is sanitized and bounded.

## Definition of done

- A source that fails is distinguishable from a healthy source returning zero jobs.
- Every rejected job increments exactly one primary rejection reason.
- The sum of accepted plus primary rejection counts equals the raw count before database deduplication.
- `/health` exposes enough data to diagnose missing results.
- Existing notification behavior remains unchanged in this phase.
- Full tests pass.

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

- Date:
- Branch/worktree:
- Commit:
- Existing test result:
- Docker build:
- Peak memory:
- Health output summary:
- Notes:

## Phase 1

- Status: Not started
- Commit:
- Tests:
- Peak memory:
- Notes:

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
