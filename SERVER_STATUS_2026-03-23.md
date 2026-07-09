# Server Status Report — 23 March 2026

## 🟢 Bot Status: HEALTHY & RUNNING

| Metric | Value |
|--------|-------|
| **Container Status** | `running` (healthy) |
| **Uptime** | 14+ minutes (since 08:19 UTC) |
| **Restarts** | **0** ✅ |
| **OOM Killed** | No |
| **Exit Code** | 0 (clean) |
| **Server Uptime** | 7 days, 13 hours |

---

## 💾 Memory & Resources

| Metric | Value |
|--------|-------|
| **Container Memory** | 9.5 MiB / 504 MiB (1.9%) — idle after scan |
| **System RAM** | 504 MiB total, 264 MiB available |
| **Swap** | 2.5 GiB total, 393 MiB used |
| **Disk** | 14 GB used / 30 GB (47%) |
| **Load Average** | 0.32, 1.52, 13.08 |

---

## ⚙️ Configuration

| Setting | Value |
|---------|-------|
| Scan Interval | 45 min |
| Digest Interval | 6 h |
| Max Concurrent Sources | 6 (server .env) / 3 (code default) |
| Sources Registered | **17** |
| Health Port | 8080 |
| Log Level | INFO |

---

## 🔍 Latest Scan Results

| Metric | Value |
|--------|-------|
| **Scan Time** | 08:20:02 → 08:23:11 UTC (~3 min) |
| **Raw Jobs Fetched** | 1,531 |
| **After Filters** | 122 accepted |
| **After Dedup** | 106 new |
| **Saved to DB** | 106 |
| **Sent to Discord** | 106/106 ✅ |
| **Next Scan** | ~45 min from last scan |

### Source Breakdown

| Source | Raw Jobs | Notes |
|--------|----------|-------|
| stepstone | 310 | ✅ Arbeitsagentur API |
| hours80k | 300 | ✅ Algolia (3 pages) |
| themuse | 200 | ✅ |
| nofluffjobs | 153 | ✅ **Now paginated** (2 pages, ~4 MB vs old 151 MB) |
| weworkremotely | 117 | ✅ 5 RSS feeds |
| arbeitnow | 100 | ✅ |
| remoteok | 95 | ✅ |
| landingjobs | 50 | ✅ |
| idealist | 44 | ✅ |
| linkedin | 35 | ✅ 4 queries |
| eurobrussels | 35 | ✅ |
| goodjobs | 25 | ✅ |
| remotive | 24 | ✅ 3 categories |
| himalayas | 23 | ✅ |
| devex | 20 | ✅ |
| reliefweb | 0 | ⚠️ Feed returned empty |
| techjobsforgood | 0 | ❌ 403 Forbidden (blocked) |

---

## 📊 Database Stats

| Metric | Value |
|--------|-------|
| **Total Jobs in DB** | 156 |
| **Jobs Added Today** | 106 |

### Jobs by Source (all time)

| Source | Count |
|--------|-------|
| remoteok | 33 |
| stepstone | 28 |
| weworkremotely | 21 |
| hours80k | 18 |
| linkedin | 16 |
| arbeitnow | 12 |
| himalayas | 9 |
| nofluffjobs | 8 |
| remotive | 5 |
| themuse | 4 |
| idealist | 1 |
| devex | 1 |

---

## 📬 All 106 Jobs Sent to Discord

### himalayas (9 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | 524 \| Senior Data Support Engineer | Intetics | eu | — |
| 2 | CAD & Engineering Design Experts | G2i | eu | 312,000–520,000 USD |
| 3 | Electrodynamics Engineer - Remote | mercor | eu | 145,600–187,200 USD |
| 4 | Energy Engineering & Python Expert - Freelance AI Trainer | Mindrift | eu | 68,640 USD |
| 5 | Python Developer Remote | mercor | eu | 208,000 USD |
| 6 | Quantitative Analyst | Eqvilent | worldwide | — |
| 7 | Senior Devops Engineer (REF3966G) | Deutsche Telekom IT Solutions | eu | — |
| 8 | Senior Identity Engineer | Sophos | eu | — |
| 9 | Staff Engineer (Platform Architecture, JetBrains Cloud Platform) | JetBrains | eu | — |

### hours80k (18 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | AI Safety Research Accelerator | Meridian | worldwide | — |
| 2 | AI Security Researcher | Carnegie Mellon University, SEI | worldwide | — |
| 3 | AICRAFT Program | AE Studio | worldwide | — |
| 4 | Benchside Software Engineer, Wet Lab | PopVax | worldwide | — |
| 5 | Data Engineer, Safeguards | Anthropic | worldwide | £170,000–£220,000 |
| 6 | Mathematical Modeller, Vaccine-Preventable Diseases | EU ECDC | eu | — |
| 7 | Member of Technical Staff, Research | METR | worldwide | $250,000–$450,000 |
| 8 | Member of Technical Staff, Synthetic Data | Trajectory Labs | worldwide | $125,000–$225,000 |
| 9 | Request for Proposals, AI Interpretability (2026) | Schmidt Sciences | worldwide | — |
| 10 | Request for Proposals, Red Team, Lie Detection Competition | Cadenza Labs | worldwide | $10,000–$25,000 stipend |
| 11 | Research Engineer | FAR AI | worldwide | $50–$100/hr |
| 12 | Research Engineer, Societal Impacts | UK AI Security Institute | worldwide | £65,000–£145,000 |
| 13 | Research Laboratory Technician, Centre for Climate Repair | Cambridge University | worldwide | £33,951–£39,906 |
| 14 | Researcher, Loss of Control | OpenAI | worldwide | $295,000–$445,000 |
| 15 | Security Labs Engineer | Anthropic | worldwide | $320,000–$405,000 |
| 16 | Senior Security Engineer | Apollo Research | worldwide | £130,000–£200,000 |
| 17 | Staff Applied Research and ML, Responsible AI and Safety | Apple | worldwide | $212,000–$386,300 |
| 18 | Test Specialist, Quality Engineering (2026) | IBM | worldwide | $45,427–$83,823 |

### linkedin (16 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Back End Developer | AllCloud | eu | — |
| 2 | Backend Developer | Deloitte | eu | — |
| 3 | Backend Developer | SII Group Romania | eu | — |
| 4 | Backend Developer | Deloitte | eu | — |
| 5 | Backend Developer | GBT Solutions, Lda | eu | — |
| 6 | Front End Software Engineer | Ciklum | eu | — |
| 7 | Front End Software Engineer | Ciklum | eu | — |
| 8 | Frontend Developer | caravanmarkt24 | germany | — |
| 9 | Full Stack Developer \| Remote | Crossing Hurdles | germany | — |
| 10 | Full Stack Engineer | Metrikflow | germany | — |
| 11 | Full Stack Software Engineer (Frontend-Leaning) | Uthereal | eu | — |
| 12 | Junior Backend Engineer | Volkswagen Digital:Hub | eu | — |
| 13 | Junior Node.js Backend Developer | Lite e-Commerce | eu | — |
| 14 | Software Developer (Remote) - Java / Atlassian | Accxia | germany | — |
| 15 | Software Engineer (Frontend) \| Remote | Crossing Hurdles | germany | — |
| 16 | Web Developer | JOBSTODAY.WORLD | germany | — |

### nofluffjobs (8 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Databricks Data Engineer | Link Group | eu | 5,208–6,384 EUR/mo (b2b) |
| 2 | Forward Deployed Engineer with German | Shelf | eu | 6,927–10,390 EUR/mo (b2b) |
| 3 | Senior Full-Stack (Python/Angular) Engineer | Matrix Global Services | eu | 7,500–8,500 EUR/mo (b2b) |
| 4 | Senior Java Backend Engineer (Core Java, Trading Systems) | Vistulo | eu | 8,568–9,408 EUR/mo (b2b) |
| 5 | Senior Java Backend Engineer (Core Java, Trading Systems) | Vistulo | eu | 8,568–9,408 EUR/mo (b2b) |
| 6 | Software Design Engineer | Evertz | eu | 4,910–5,377 EUR/mo (b2b) |
| 7 | Software Engineer | SquareOne | eu | 5,040–6,552 EUR/mo (b2b) |
| 8 | Software Engineer, iOS Core Product | Speechify | eu | 4,329–8,658 EUR/mo (b2b) |

### stepstone (28 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | C++ Software Developer for Speech Recognition | alfatraining Bildungszentrum | germany | — |
| 2 | DevOps Engineer CI/CD (m/w/d) | Atruvia AG | germany | — |
| 3 | IT Solution Architect - Kubernetes (w/m/d) | Bechtle AG | germany | — |
| 4 | IT-Application Engineer | Spell GmbH | germany | — |
| 5 | Junior Developer m/w/d | Proalpha Group GmbH | germany | — |
| 6 | Kubernetes Engineering Consultant (w/m/d) | Bechtle AG | germany | — |
| 7 | Linux System Admin / DevOps Engineer (m/w/d) | NetCologne IT Services | germany | — |
| 8 | PHP Symfony Backend Developer Webshop (m/w/d) | Hygi.de GmbH | germany | — |
| 9 | Principal Software Engineer (m/w/d) – JVM & Cloud | zollsoft GmbH | germany | — |
| 10 | Python Developer (m/w/d) - Backend & Data Engineering | Stiftung Kirchliches Rechenzentrum | germany | — |
| 11 | Quantum Software Engineer (m/f/d) | GWDG Göttingen | germany | — |
| 12 | SOFTWARE DEVELOPER AL / C/AL / MS Navision BC 365 | Markmann + Müller Datensysteme | germany | — |
| 13 | Security Engineer | Hornetsecurity GmbH | germany | — |
| 14 | Senior Backend / Full-Stack Developer - AI & Cloud | Goalscape Software | germany | — |
| 15 | Senior Backend Developer - C#.NET (m/w/d) | tef-Dokumentation GmbH | germany | — |
| 16 | Senior Backend Engineer Video/Realtime (m/w/d) | BWI GmbH | germany | — |
| 17 | Senior DevOps / Internal Platform Developer (m/w/d) | Venios GmbH | germany | — |
| 18 | Senior DevOps / Internal Platform Developer (m/w/d) | Venios GmbH | germany | — |
| 19 | Senior Developer / Architect m/w/d | Proalpha Group GmbH | germany | — |
| 20 | Senior Full Stack Developer (m/w/d) | WESTPRESS GmbH | germany | — |
| 21 | Senior Full Stack Web Developer – React (m/w/d) | Greenware GmbH | germany | — |
| 22 | Senior Fullstack Developer - Java EE/Spring/Angular/React | CodeCamp:N GmbH | germany | — |
| 23 | Senior Professional Test Engineer (w/m/d) | Bundesagentur für Arbeit IT-Systemhaus | germany | — |
| 24 | Senior Python Software Engineer (m/w/d) | Tenhil GmbH | germany | — |
| 25 | Senior Software Engineer [m/w/d] Java / Spring Boot | Die Tech Recruiter GmbH | germany | — |
| 26 | Software Developer | Hornetsecurity GmbH | germany | — |
| 27 | Software Engineer - Input Management (m/w/d) | ERGO Direkt AG | germany | — |
| 28 | Lead Developer / Fullstack-Entwickler (m/w/d) — Vue/Nuxt/TS | Motion Media GmbH | germany | — |

### themuse (4 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Principal ML Engineer - Embodied AI Scaling Foundations | General Motors | worldwide | — |
| 2 | Principal Offensive Security Developer | Autodesk | worldwide | — |
| 3 | Senior Software Engineer I - External Platform | Samsara Inc. | worldwide | — |
| 4 | Senior Software Engineer II - External Platform | Samsara Inc. | worldwide | — |

### weworkremotely (21 jobs)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Design Engineer, Design Systems | Livekit | worldwide | — |
| 2 | Forward Deployed Engineer | Arch Works | worldwide | — |
| 3 | Frontend Developer | C4Media | worldwide | — |
| 4 | Full Stack Developer (RoR/React/React Native) | Metova | worldwide | — |
| 5 | Full Stack Engineer | Ivy Tech | eu | — |
| 6 | Full-Stack Developer | ELECTE S.R.L. | worldwide | — |
| 7 | Full-Stack Engineering Lead | Rare Days | worldwide | — |
| 8 | Java Developer - AI (Backend) | CloudDevs | worldwide | — |
| 9 | Lead Product Architect | Jobgether | worldwide | — |
| 10 | Product Designer, Growth | Livekit | worldwide | — |
| 11 | Product Operations Lead | Apollo Graphql | worldwide | — |
| 12 | Product Owner (m/w/d) Softwareentwicklung | Skalbach | worldwide | — |
| 13 | SVP, Data Product & Insights - Life Sciences | Arcadia | worldwide | — |
| 14 | Senior Full Stack Developer (Kotlin, Vue.js) | Smart Working Solutions | worldwide | — |
| 15 | Senior Software Engineer | Mangomint | worldwide | — |
| 16 | Senior Software Engineer | Strange Loop Labs | worldwide | — |
| 17 | Simplified Chinese Marketing and Product Remote Linguist | Welocalize | worldwide | — |
| 18 | Sr. Product Capability Architect - REMOTE | Jobgether | worldwide | — |
| 19 | Sr. Product Designer - Consumer | Kraken | worldwide | — |
| 20 | Talent Team Lead, Product, Design, & Engineering | Anchorage Digital | worldwide | — |
| 21 | WordPress Support Engineer | Yoko Co | worldwide | — |

### remoteok (1 job)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Lead 3D Environment Artist | Eleventh Hour Games | worldwide | — |

### arbeitnow (1 job)

| # | Title | Company | Scope | Salary |
|---|-------|---------|-------|--------|
| 1 | Commissioning Engineer | ILOS Projects GmbH | eu | — |

---

## 🩺 Health Endpoint

```json
{
  "status": "ok",
  "uptime_seconds": 815,
  "last_scan": "2026-03-23T08:23:11.564993+00:00",
  "jobs_tracked": 156,
  "next_scan_in_seconds": 2700
}
```

---

## 🔧 Git Status (Server)

```
cd74cbe fix: OOM crash — NoFluffJobs paginated API + reduce concurrency
d5d75f7 fix: rewrite event loop to fix 'already running' crash
bb26e0a v1.5: Add 4 new sources, rebuild CI/CD, add server aliases
75b21dd v1.5: Remove Playwright, add LinkedIn + Stepstone, fix filters & digest
291ef9a fix deploy: increase SSH timeout to 30m, skip Playwright in Docker build
```

---

## ⚠️ Known Issues

1. **techjobsforgood** — 403 Forbidden (site is blocking the bot's user agent)
2. **reliefweb** — returned 0 jobs this scan (intermittent feed issue)
3. **Server .env** still has `MAX_CONCURRENT_SOURCES=6` (works fine now since NoFluffJobs is paginated; code default is 3)
4. **Discord rate limits** — 8 rate-limit hits during the 106-job batch send (all recovered automatically)
