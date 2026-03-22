# Job Tracker Bot — v1.4: Critical Fixes + Volume Investigation

The bot has been running 2 days on Oracle Cloud and results are poor:
- Only 50 jobs tracked in 48 hours across 6 sources (way too low)
- Wrong jobs getting through (Product Manager, Go-To-Market, Web3)
- NGO misclassification (TestGorilla flagged as NGO — it's a SaaS company)
- Match score showing "not scored" on most jobs (pipeline bug)
- Digest repeating the same jobs every 6 hours (not marking as notified)

Fix everything below in order. Do NOT move to the next step until
the current one is verified working.

---

## STEP 1 — DIAGNOSE: Why only 50 jobs in 48 hours?

First, understand the volume problem before fixing filters.

Run this on the server and show me the full output:

```bash
sudo docker compose exec job-bot python main.py --dry-run --verbose 2>&1 | head -200
```

Then run per-source to see how many each source is finding and rejecting:

```bash
sudo docker compose exec job-bot python main.py --dry-run --source remotive --verbose
sudo docker compose exec job-bot python main.py --dry-run --source arbeitnow --verbose
sudo docker compose exec job-bot python main.py --dry-run --source remoteok --verbose
sudo docker compose exec job-bot python main.py --dry-run --source weworkremotely --verbose
sudo docker compose exec job-bot python main.py --dry-run --source idealist --verbose
sudo docker compose exec job-bot python main.py --dry-run --source reliefweb --verbose
```

Report the raw→accepted counts for each source.
Also check logs for any errors:
```bash
sudo docker compose logs --tail=200 | grep -E "ERROR|WARNING|Failed|Exception"
```

---

## STEP 2 — FIX: Role filter letting wrong jobs through

These job types are appearing in Discord and must be rejected:

**Add to ROLE_REJECT_KEYWORDS (title check only — NOT description):**
```python
# Sales/marketing hybrids
"go to market", "go-to-market", "gtm engineer",

# Product roles (not engineering)
"product manager", "senior product manager", "staff product manager",
"principal product manager", "head of product",

# Web3/blockchain (not in user's stack)
"smart contract", "blockchain engineer", "web3 engineer",
"solidity", "defi engineer", "crypto engineer",
"svm engineer",  # Solana Virtual Machine

# Vague leadership without tech — only reject pure "tech lead"
# if no stack keywords found in title or tags
```

**IMPORTANT:** Only check TITLE and TAGS for reject keywords.
Do NOT check description — a fullstack dev job description will
naturally mention words like "sales tools", "marketing dashboard"
and those should not trigger rejection.

**Add company blocklist to .env and config.py:**
```
COMPANY_BLOCKLIST=TechBiz Global,Lemon.io,A.Team,Zipdev,Turing,Toptal
```

Implement in _apply_filters() BEFORE all other filters.
Log: "[source] Rejected: {title} at {company} (company blocklist)"

---

## STEP 3 — FIX: NGO misclassification

TestGorilla (hiring assessment SaaS) is being classified as 🏛️ NGO.
This is wrong. Find and fix the root cause.

**Debug first — add score logging to ngo.py:**
```python
logger.debug(
    "[ngo] {}: company_kw={}, desc_kw={}, known_list={}, "
    "penalties={}, total={} → is_ngo={}",
    job.company, company_score, desc_score, 
    known_score, penalty, total, total >= threshold
)
```

Run --dry-run --source remoteok and find what triggered NGO for TestGorilla.

**Fix threshold logic:**
Description keywords alone must NOT be enough to classify as NGO.
Require: (company_keywords >= 1) OR (known_list match) OR 
(description_keywords >= 2 AND company_keywords >= 1)

**Add strong NOT-NGO penalties (-3 each):**
```python
NOT_NGO_STRONG = [
    "assessment platform", "hiring platform", "recruiting platform",
    "hr software", "hrtech", "hr tech",
    "talent assessment", "skills testing", "skills assessment",
    "saas platform", "b2b saas",
    "series a funded", "series b funded", "venture backed",
]
```

**Add these tests:**
- TestGorilla → is_ngo=False
- Anthropic → is_ngo=False
- Shopify → is_ngo=False
- Mozilla Foundation → is_ngo=True
- Amnesty International → is_ngo=True
- UNHCR → is_ngo=True

---

## STEP 4 — FIX: Match score showing "not scored"

Most Discord notifications show "📊 Match — not scored".
This is a bug — match scoring must run on every accepted job.

**Debug:**
1. In main.py _apply_filters(), add before returning accepted jobs:
   ```python
   logger.debug("[match] {} jobs accepted, {} with match_score set",
       len(accepted), sum(1 for j in accepted if j.match_score is not None))
   ```
2. Check the Job model default for match_score — is it None or 0?
3. Check if MatchScorer is instantiated and called in _apply_filters()

**Fix:**
- match_score must default to 0 in Job model (not None)
- MatchScorer.score(job) must be called for every job in accepted list
- If scoring raises any exception, catch it, log warning, set score=0
- In discord_notifier.py, render score=0 as "0% match" not "not scored"
  Only show "not scored" if match_score is None (which should never happen
  after this fix)

---

## STEP 5 — FIX: Digest repeating same jobs

The same 3 jobs appear in every 6-hour digest. Fix two things:

**Fix 1 — Mark digest jobs as notified after sending:**
In database.py add:
```python
async def mark_jobs_notified(self, job_ids: list[str]) -> None:
    async with aiosqlite.connect(self.db_path) as db:
        await db.executemany(
            "UPDATE jobs SET notified = 1 WHERE id = ?",
            [(jid,) for jid in job_ids]
        )
        await db.commit()
```

Call this in main.py after digest is sent successfully.

**Fix 2 — Digest query must only look at last 6 hours:**
```sql
SELECT * FROM jobs 
WHERE notified = 0 
AND fetched_at > datetime('now', '-6 hours')
ORDER BY match_score DESC, fetched_at DESC
LIMIT 10
```

---

## STEP 6 — LOOSEN filters to increase volume

50 jobs in 48 hours from 6 sources is critically low.

### 6a — Remote-only boards: unknown scope → ACCEPT

For these boards, if is_remote=True AND scope=unknown → ACCEPT as worldwide:
- remoteok (it's called Remote OK, everything is remote)
- weworkremotely (remote-only platform)
- remotive (remote-only platform)

Keep strict unknown=reject ONLY for: arbeitnow, idealist, reliefweb

### 6b — WeWorkRemotely: fetch ALL category feeds

Currently likely only fetching one RSS feed. Replace with all dev feeds:

```python
RSS_FEEDS = [
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]
```

Fetch all 5 in parallel, deduplicate by URL before returning.
This alone should 3-5x the WeWorkRemotely job count.

### 6c — Remotive: fetch multiple categories

```python
REMOTIVE_CATEGORIES = [
    "software-dev",
    "devops",
    "product", # keep only if role filter catches PM roles
]
```

URL: `https://remotive.com/api/remote-jobs?category=software-dev&limit=100`

### 6d — Add more ACCEPT role keywords:

```python
# Missing from current list — add these:
"software development engineer",
"web application developer",
"api developer", "api engineer",
"platform developer", "platform engineer",
"solutions engineer",
"integration engineer",
"technical lead",  # remove from reject list — keep with stack check
"staff engineer",
"principal engineer",
"site reliability engineer", "sre",
"cloud engineer",
"systems engineer",
"application developer",
"application engineer",
```

---

## STEP 7 — Add Otta / Welcome to the Jungle source

One of the best EU remote tech job boards. Many companies post
exclusively here. Strong for Berlin/EU remote roles.

**Source file:** `sources/otta.py`
**Method:** httpx + BeautifulSoup
**URL:** `https://jobs.welcometothejungle.com/en/jobs?refinementList%5Bremote%5D%5B0%5D=fulltime&page=1`

Or try their API endpoint:
`https://api.welcometothejungle.com/api/v1/jobs?remote=fulltime&page=1`

Parse: title, company, location, tags, URL, salary if present.
Location is usually well-structured (city + country).
Not NGO-specific — use standard NGO classifier.

Expected yield: 30-60 raw jobs, 10-20 accepted after filters.

---

## STEP 8 — Add Stepstone.de source (German market)

Germany's largest job board. Essential for German market coverage.

**Source file:** `sources/stepstone.py`
**Method:** httpx + BeautifulSoup
**URL:** `https://www.stepstone.de/jobs/entwickler/in-deutschland?radius=30&remoteOnly=true`

Parse job cards. Note: postings may be in German.
For Stepstone specifically:
- Accept English-language postings normally
- For German-language postings: accept if company is international
  AND role keywords match in either language
  (add German role keywords: "Entwickler", "Ingenieur", "Software")
- Location: if remote mentioned → accept, scope=germany

Expected yield: 20-40 raw jobs, 5-15 accepted.

---

## STEP 9 — LinkedIn Jobs (attempt, may get blocked)

Try LinkedIn public job search. It often blocks scrapers but worth trying.

**Source file:** `sources/linkedin.py`
**Method:** httpx with realistic headers
**URL:**
```
https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=full+stack+developer&location=Germany&f_WT=2&f_TPR=r604800&start=0
```

Parameters: f_WT=2 = remote, f_TPR=r604800 = last 7 days

LinkedIn has a guest API that sometimes works without auth.
If it returns 429 or redirect to login → log warning and return [].
Do NOT use Playwright for LinkedIn — against ToS and will get IP banned.

If the guest API works, also try:
```
keywords=software+engineer&location=Europe&f_WT=2
keywords=fullstack+developer&location=Berlin&f_WT=2
```

---

## STEP 10 — Verification after all fixes

Run full dry-run and verify:

```bash
sudo docker compose exec job-bot python main.py --dry-run --verbose 2>&1
```

Expected after all fixes:
- 150-300 raw jobs fetched per scan (up from ~50 total in 48h)  
- 20-40 accepted per scan
- Zero Product Manager / GTM / Smart Contract jobs in results
- Zero TestGorilla-type NGO false positives
- Every accepted job has a numeric match score
- After running twice, second run shows 0 new (dedup working)
- Digest shows only new jobs since last digest

---

## Build order (strict — verify each before next)

1. DIAGNOSE — run verbose dry-run per source, report counts
2. Fix digest repeat (Step 5) — 15 min fix, high annoyance
3. Fix match score not-scored (Step 4) — 15 min fix
4. Fix NGO misclassification (Step 3) — add debug logging first
5. Fix role filter (Step 2) — reject GTM/PM/Web3, add blocklist
6. Loosen filters + WeWorkRemotely feeds (Step 6) — volume fix
7. Rebuild Docker and deploy: git push → CI/CD auto-deploys
8. Watch Discord for 2 hours — confirm quality improved
9. Add Otta source (Step 7)
10. Add Stepstone (Step 8)
11. Try LinkedIn (Step 9)
12. Final verification

---

## Test requirements

All existing tests must pass throughout.
Add:
- TestGorilla → is_ngo=False
- "Go to Market Engineer" → role rejected
- "Senior Product Manager" → role rejected
- "Smart Contract Engineer SVM" → role rejected
- TechBiz Global → company blocklist rejected
- match_score is never None after pipeline runs
- remoteok + is_remote=True + scope=unknown → accepted
- WeWorkRemotely fetches 5 feeds not 1
- Digest does not repeat jobs after being sent

Target: 340+ tests
