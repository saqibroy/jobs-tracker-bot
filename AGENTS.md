# AGENTS.md

## Project mission

Maintain a lightweight Python job-tracking bot for a Berlin-based full-stack/frontend developer. The bot prioritizes jobs that can legally and practically be performed from Germany, including Berlin hybrid/on-site roles and explicitly Germany/EU/EMEA/worldwide remote roles.

The production service runs in Docker on a 512 MB Oracle Cloud free-tier server.

## Working agreements

- Follow DRY, KISS, and YAGNI.
- Prefer small, reviewable changes over broad rewrites.
- Preserve existing behavior unless the active implementation plan explicitly changes it.
- Do not invent requirements that are not in `IMPLEMENTATION_PLAN.md`, `profile.toml`, tests, or the user’s prompt.
- Do not add production dependencies without explaining why existing packages or the standard library are insufficient.
- Do not change secrets, credentials, tokens, `.env`, private mail data, or production database files.
- Never log email bodies, OAuth tokens, webhook URLs, or other sensitive content.
- Keep source failures isolated so one platform cannot crash a scan.
- Use bounded concurrency and avoid retaining large response bodies longer than necessary.
- Production must work without Playwright/Chromium.
- Keep SQLite. Use explicit, idempotent migrations.
- Do not introduce Redis, PostgreSQL, Celery, an ORM, a dashboard, or another service unless a future approved plan explicitly requires it.

## Runtime constraints

- Docker memory limit: 512 MB.
- Target peak memory: below 430 MiB.
- Default production concurrency should remain conservative.
- Prevent overlapping scheduled and manually triggered scans.
- Clean up or bound persisted metrics/history.
- Keep health responses compact.

## Eligibility invariants

- Remote jobs must explicitly allow Germany, Europe/EU/EEA/EMEA/DACH, or worldwide.
- Country-restricted remote roles outside Germany remain rejected.
- Hybrid and on-site roles are accepted only when the workplace is Berlin.
- Unknown work eligibility remains rejected rather than guessed.
- Work eligibility and CV match are separate decisions.

## Candidate profile behavior

- Primary roles: full-stack and frontend.
- Strongly aligned backend/web roles may pass when the stack matches.
- Working-student, internship, junior, and clearly unrelated roles remain rejected unless `profile.toml` changes.
- The user is open to full-time, part-time, fixed-term, contract-employment, and freelance work.
- Freelance roles may require separate authorization; classify and flag them rather than silently mixing them with employment roles.
- Explicit German requirements above the configured B1 maximum must reject.
- A posting written in German is not by itself a rejection reason.

## Code conventions

- Use typed Python.
- Prefer dataclasses or Pydantic models for stable domain data.
- Prefer stable reason/status codes plus human-readable explanations.
- Keep business logic outside CLI formatting and notifier rendering.
- Keep provider-specific parsing inside source/integration modules.
- Avoid circular imports.
- Keep async functions non-blocking.
- Bound text stored in metrics and error fields.
- Do not duplicate employment, language, or eligibility logic across adapters.

## Database rules

- Migrations must be safe to run repeatedly.
- Existing databases must continue to start automatically.
- Schema changes require migration tests.
- Deduplication by URL ID and content hash must remain intact.
- Notification state changes must be idempotent.
- Do not share SQLite across machines or network filesystems.

## Testing

For a changed phase:

1. Run focused tests first.
2. Run:
   - `python -m pytest tests/v2 -q`
   - `python -m pytest -q --timeout=30`
3. Build the Docker image.
4. Run a representative dry scan.
5. Validate `/health`.
6. Record memory when runtime behavior changes.

Mock external HTTP in automated tests. Do not make the normal unit test suite depend on live job platforms.

## Planning and progress

- Read `IMPLEMENTATION_PLAN.md` before work.
- Implement one approved phase at a time.
- Update phase checkboxes and the progress log.
- Do not mark a task complete without verification.
- Do not start the next phase in the same run unless explicitly requested.
- End every implementation run with files changed, tests run, results, memory observations, limitations, and the next phase recommendation.

## Git

- Use a feature branch or worktree.
- Keep commits focused by phase.
- Do not force-push or rewrite unrelated history.
- Do not commit generated databases, logs, credentials, private mail files, or local exports.
