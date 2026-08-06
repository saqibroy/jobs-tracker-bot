# Codex Workflow Prompts — Job Tracker Bot

Attach this file together with `IMPLEMENTATION_PLAN.md` when starting the work.

Use the prompts in order. Do not ask Codex to implement the entire roadmap in a single unreviewed run.

---

# Recommended Codex settings

## Planning pass

- Model: **GPT-5.6 Sol**
- Reasoning: **Extra High**
- Speed: **Standard**
- Environment: **Worktree**
- Mode: `/plan`

## Implementation of Phase 1–3

- Model: **GPT-5.6 Sol**
- Reasoning: **High** or **Extra High**
- Speed: **Standard**
- Environment: the same worktree
- Mode: `/goal`

## Clear follow-up tasks and repetitive test work

- Model: **GPT-5.6 Terra**
- Reasoning: **High**
- Speed: **Standard**

Use Luna only for highly specified, repetitive transformations after the architecture and acceptance criteria are settled.

Do not use Ultra for the first implementation run. Multiple delegated agents are useful for independent analysis, but this roadmap changes shared central files and should be implemented sequentially.

Use Fast mode only when latency matters enough to justify higher credit usage. Standard is appropriate for this project.

---

# Prompt A — Planning review

Start Codex in the repository worktree, attach `IMPLEMENTATION_PLAN.md`, and enter `/plan`. Then paste:

```text
Review this repository and the attached IMPLEMENTATION_PLAN.md.

Goal:
Produce a safe, repo-specific execution plan for improving job coverage and recall while keeping the production Docker service within its 512 MB memory limit.

Important context:
- This is a Python async job tracker deployed through Docker Compose to an Oracle Cloud free-tier server.
- The current architecture includes source adapters, filters, SQLite, Discord/Telegram notifications, a health endpoint, GitHub Actions deployment, and optional read-only Zoho Mail ingestion.
- The user is now open to full-time, part-time, fixed-term, contract-employment, and freelance opportunities.
- Germany/Berlin eligibility must remain strict.
- German-language job descriptions must not be rejected merely because they are written in German; explicit German requirements above B1 must still reject.
- Do not add Playwright/Chromium to production.
- Prefer job-alert email ingestion over authenticated scraping of LinkedIn or Indeed.
- Follow DRY, KISS, and YAGNI.
- Preserve existing behavior unless the plan explicitly changes it.
- Do not expose or modify secrets.

Your task in this planning pass:
1. Read the repository, especially main.py, config.py, profile.toml, models/job.py, filters/, sources/base.py, storage/, integrations/zoho_mail.py, health.py, notifiers/, tests/, Docker files, and GitHub workflows.
2. Compare the attached plan against the actual current code.
3. Identify incorrect assumptions, hidden coupling, migration risks, memory risks, and test/CI discrepancies.
4. Edit IMPLEMENTATION_PLAN.md only where needed to make it executable and accurate.
5. Break oversized tasks into smaller reviewable steps.
6. Add exact file/module targets to Phase 1.
7. Add measurable acceptance criteria and commands.
8. Do not modify production code in this planning pass.
9. Do not start implementation.
10. Finish with:
   - findings
   - plan changes
   - unresolved risks
   - recommended first implementation goal

Definition of done:
- IMPLEMENTATION_PLAN.md accurately reflects the current repository.
- Phase 1 can be implemented without guessing.
- No product code is changed.
```

Review Codex’s updated plan before starting implementation.

---

# Prompt B — Start Phase 0 and Phase 1

After accepting the plan, enter `/goal` and paste:

```text
Implement Phase 0 and Phase 1 from IMPLEMENTATION_PLAN.md only.

Outcome:
Create a baseline and add lightweight, production-safe source/pipeline observability so I can tell whether jobs are missing because a source failed, returned zero results, or because filters rejected them.

Constraints:
- Production memory limit is 512 MB.
- Target peak container memory below 430 MiB.
- Do not add Playwright, Chromium, Redis, PostgreSQL, an ORM, a dashboard, or a second service.
- Avoid new production dependencies unless clearly necessary.
- Preserve strict Germany/Berlin eligibility.
- Preserve current notification routing in this phase.
- Preserve existing CLI behavior, including --dry-run, --source, --verbose/--explain, --stats, --validate-sources, and Zoho commands.
- Preserve SQLite compatibility using idempotent migrations.
- One failed source must not crash the scan.
- Do not expose secrets or email bodies.
- Follow AGENTS.md and IMPLEMENTATION_PLAN.md.
- Do not start Phase 2.

Required work:
1. Complete and record Phase 0 baseline.
2. Implement stable source outcome statuses.
3. Implement stable filter rejection reason codes.
4. Aggregate per-source funnel metrics.
5. Persist a bounded source-scan history.
6. Extend /health, --stats, and the daily status summary.
7. Add/adjust tests.
8. Run targeted and full tests.
9. Build and run Docker.
10. Capture representative memory usage.
11. Update IMPLEMENTATION_PLAN.md checkboxes and progress log.
12. Make one focused commit for Phase 1.

Verification:
- Healthy zero-result sources differ from failures.
- Raw count equals accepted plus primary rejection counts before DB dedup.
- Every source has a last status and last successful timestamp.
- Existing job notifications have not changed.
- Full tests pass.
- Docker health check passes.
- Peak memory is recorded.

At the end, report exactly:
- files changed
- schema changes
- tests and commands run
- results
- memory observation
- known limitations
- commit hash
- recommended next goal
```

---

# Prompt C — Execute the next approved phase

Use this reusable prompt after reviewing each completed phase:

```text
Implement Phase <NUMBER> from IMPLEMENTATION_PLAN.md only.

Before editing:
1. Read AGENTS.md and the current plan.
2. Inspect the current branch and previous phase changes.
3. Confirm the phase definition of done against the actual code.
4. Identify any plan item that is now stale and update the plan before implementation.

Implementation rules:
- Make the smallest coherent change that completes this phase.
- Do not start later phases.
- Preserve backward compatibility unless the phase explicitly changes behavior.
- Add idempotent SQLite migrations for schema changes.
- Add targeted tests and run the full CI test command.
- Build the Docker image.
- Run a representative dry scan.
- Record memory/runtime observations.
- Update the plan checklist and progress log.
- Commit the phase separately.

At the end, report:
- phase completed
- files changed
- key decisions
- migrations
- tests/commands and results
- memory/runtime observations
- known limitations
- commit hash
- exact next recommended phase
```

Replace `<NUMBER>` with the approved phase.

---

# Prompt D — Review a completed phase before moving on

Use a separate Codex review chat or review mode when possible:

```text
Review the current branch changes for Phase <NUMBER> against AGENTS.md and IMPLEMENTATION_PLAN.md.

Do not implement new features.

Check specifically:
- correctness
- backward-compatible SQLite migrations
- async/concurrency safety
- scan overlap
- deduplication correctness
- notification idempotency
- error classification
- secret/privacy exposure
- 512 MB memory risks
- unnecessary dependencies
- test quality
- mismatch between docs, CI, and runtime behavior

Run relevant tests if available.

Return findings ordered by severity with file and line references. Clearly state whether the phase is safe to merge and what must be fixed first.
```

---

# Prompt E — Fix review findings

```text
Address only the approved review findings for Phase <NUMBER>.

Constraints:
- Do not broaden scope.
- Do not start another phase.
- Preserve the accepted phase design.
- Add regression tests for each bug fixed.
- Run targeted and full tests.
- Update IMPLEMENTATION_PLAN.md only if the finding changes a verified assumption.
- Commit fixes separately with a focused message.

Report each finding and how it was resolved.
```

---

# Suggested Git workflow

```bash
git checkout main
git pull --ff-only
git worktree add ../jobs-tracker-coverage -b feat/coverage-and-employment-types
cd ../jobs-tracker-coverage
```

Commit suggestions:

```text
chore: record coverage improvement baseline
feat: add source scan funnel observability
feat: classify employment types and contract metadata
feat: make language filtering requirement-aware
feat: add explore routing and employment digests
feat: schedule source groups within memory limits
feat: ingest job alert emails from zoho
feat: add lightweight job sources
docs: document source coverage and production tuning
```

Do not combine all of these into one commit or one pull request unless the changes remain small and independently reviewable.
