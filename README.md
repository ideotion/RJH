# RJH — Reverse Job Hunting

> The search should run both ways. RJH finds the roles, tailors your documents with a local AI, and pre-fills the forms — leaving you only the final review and the submit click.

RJH is an ethical, open-source, local-first job-application copilot. Everything runs on your machine. Nothing about you is sent to any cloud service, and the tool never submits an application on its own — a human gives the final go, every time.

**Status:** early but working MVP. Runs offline out of the box via a demo source so you can try the full workflow before wiring real feeds.

**License:** GNU GPLv3.

## Why this exists

Job hunting is built backwards. A qualified person fires off hundreds of applications into automated funnels and mostly hears nothing back — the silence usually says more about broken hiring pipelines than about the candidate. Meanwhile companies pay recruiters and sourcers precisely because finding the right people is supposed to be *their* job too.

RJH tilts the effort back toward you. It does the repetitive parts — searching, matching, drafting, form-filling — so your energy goes where it matters: deciding which roles are worth your name, and putting your real judgement on the final send.

The principles RJH is built on live in [MANIFESTO.md](MANIFESTO.md).

## What it does

1. Collects job postings several ways: config-driven source adapters (RSS/Atom feeds and JSON job APIs); a built-in **directory of ~180 European job sources** that RJH can probe to discover a usable feed — RSS/Atom autodiscovery, known JSON/ATS endpoints (Greenhouse, Lever, Ashby, …) and common feed paths — and that you then promote into a live source; **job-alert email** (IMAP/POP3, or a dragged-in `.eml`) — the boards that forbid scraping will happily email you matching roles, and reading those breaks no Terms of Service; a **careers-page crawler** that discovers individual postings on a site or ATS; or a CSV/JSON import. Every fetch runs through one ethical pathway: an SSRF guard, robots.txt (fail-closed for auto-discovered URLs), per-domain rate limiting that honours `Crawl-delay`, and conditional GET so unchanged sources are skipped.
2. Stores them in a searchable local SQLite database with duplicate detection (URL canonicalization plus content hashing).
3. Enriches each posting locally: scores it against your master profile, parses a salary range from the text, and extracts competency tags — no AI required.
4. Lets you full-text search across title, company, location, country, salary, competencies and description, and sort by any column (score, title, company, location, salary, competencies, status) just by clicking the header.
5. *(Optional add-on)* Generates a tailored resume and cover letter per role using your local Ollama model, grounded only in your real master resume — and can also extract skills/entities, translate a non-English posting, and write an honest fit summary, all on your machine.
6. Compiles those tailored documents into formatted **OpenDocument (`.odt`)** files on demand — a built-in stdlib compiler (no third-party packages), so you get a polished, editable resume and cover letter to download.
7. Pre-fills the application form in a real browser using a per-site field-mapping system, attaching the compiled `.odt` (or your own curated file), then stops. You review, edit, and submit yourself.
8. *(Optional)* Runs the whole collect → enrich → (optionally) draft pipeline on a **background schedule**, unattended, staging everything for your review. It never submits.
9. Logs every action to the database and to a dated Markdown audit trail.

### Source directory & feed discovery

RJH ships a curated **directory of ~180 European job sources** (`sources_catalog.csv`) — public employment services, the big aggregators, country boards, and tech/life-science/executive niches — browsable on the **Sources** tab with filters for country, category and status. For each one RJH can **search for a usable feed**, cheapest method first: `<link rel="alternate">` autodiscovery in the homepage, known public JSON/ATS endpoints (Greenhouse, Lever, Ashby, Recruitee, SmartRecruiters, Personio's XML feed, and Workday's CXS API over POST), then a short list of conventional feed paths. Every candidate is **validated** — fetched through the same ethical pathway and required to parse to at least one real posting — before it is recorded, and once verified you promote it to a live source with one click.

Discovery runs as a slow, **bounded rolling sweep** inside the background scheduler (a capped batch of due sources per pass, cached so each is re-probed only every couple of weeks), or on demand from the Sources tab. It is honest about what it finds: several major boards (LinkedIn, Indeed, Glassdoor) have **no public feed** and are marked as such — for those, lean on job-alert email instead.

The scraper, email ingestion, crawler, source directory, database, search, sort, scoring, profile, resume import, export, scheduler and browser pre-fill form the **core** and run with no AI at all. The LLM document and analysis tools are an **optional add-on**, toggled in Settings; everything else keeps working when it is off.

Tuned for Western and Northern Europe. EURES (https://eures.europa.eu), the official EU job-mobility portal, is the recommended primary source; national public-employment-service feeds and company career-page feeds plug in the same way.

## Ethics (built into the code)

- A human always submits. RJH never clicks a submit button — not from a manual collection, not from the background scheduler.
- Respects robots.txt for every domain before fetching. For auto-discovered/crawled URLs it is **fail-closed**: if it cannot positively confirm a URL is allowed, it does not fetch it.
- Rate-limits itself per domain and honours any `Crawl-delay`. An SSRF guard refuses non-public targets.
- Prefers official feeds, APIs and emailed alerts over crawling, and crawls only public pages within one site.
- Email ingestion connects **read-only** and **never stores your address** — your email is read only so it can be redacted from anything kept; tracking/redirect links are unwrapped to the real posting.
- Stays local: document generation and analysis run on your own Ollama instance; your resume never leaves your machine.

Auto-submitting to sites such as LinkedIn or Indeed violates their Terms of Service and risks account bans. RJH deliberately does not do that. Reading the job alerts those sites email *to you*, however, is fine — and that is one of the sources RJH supports.

## Install

**The core has zero third-party dependencies** — it runs on the Python standard library alone (Python 3.8+), so it installs and works **fully offline**. No `pip`, no network.

One line — clones the repo, then **starts RJH and opens it in your browser** automatically:

```
curl -fsSL https://raw.githubusercontent.com/ideotion/RJH/main/install.sh | sh
```

The installer is a short, readable, GPLv3 shell script ([install.sh](install.sh)). Set `RJH_DIR=/path` to choose where it goes (default `~/rjh`), or `RJH_NO_START=1` to install without launching. To run again later: `cd ~/rjh && ./venv/bin/python rjh.py`.

Or just clone and run — nothing to install:

```
git clone https://github.com/ideotion/RJH.git && cd RJH
python3 rjh.py
```

### Optional extras

Each is opt-in and only needed for its feature; the app runs fully without them (installing them needs network once):

- `playwright` — the browser pre-fill step. After `pip install playwright`, fetch an engine with `playwright install firefox` (Firefox is the default; `chromium` and `webkit` also work, selectable in Settings).
- `pypdf` / `odfpy` — importing a resume from a PDF or ODT file (parsed locally).
- `trafilatura` — cleaner job-description text from crawled posting pages. Without it the crawler uses a built-in stdlib HTML-to-text reader, so this is purely a quality upgrade; the crawler works either way.

Email ingestion, the careers-page crawler and the background scheduler need **no extra packages at all** — they run on the standard library.

The app starts and runs fully without any of the optional packages; each feature shows a clear hint if its dependency is missing.

### Local model (Ollama)

You do not need Ollama installed to launch RJH. Open the **Settings → Setup** panel and the app will:

- detect whether the Ollama engine is installed and running, and show its version;
- offer a one-click **Install Ollama** button (Linux) that runs the official installer after you confirm — it downloads only the engine, never your data;
- list the models you already have and let you **pull** new ones (e.g. `mistral`) and pick the active one.

On non-Linux systems, install Ollama manually from https://ollama.com/download; model management still works from the panel. A multilingual-capable model is recommended, since many European postings are not in English.

## Run

```
python3 rjh.py
```

RJH starts and **opens your default browser** at http://127.0.0.1:8765 automatically. Pass `--no-browser` (or set `RJH_NO_BROWSER=1`) to skip that. Your database and audit trail live in ./rjh_data/.

## Configure

Everything is editable from the Settings tab and persisted to rjh_data/config.json.

Sources: three adapter types, all config-driven. `demo` is the bundled offline sample. `rss` reads any RSS/Atom feed. `json_api` reads any JSON jobs API — point `url` at the endpoint, set `root` to the key holding the list, and `map` your fields onto ours (`title`, `company`, `location`, `url`, `description`, `posted_at`, `salary`); map values may be **dotted paths** for nested JSON (e.g. `employer.name`, `description.text`). Two real, no-auth sources ship pre-configured but **disabled**:

- **Arbetsförmedlingen / JobTech** — Sweden's Public Employment Service open API (`jobsearch.api.jobtechdev.se`). A genuine national-PES feed; flip `enabled` to `true`.
- **Arbeitnow** — a free European job-board API.

**EURES** (https://eures.europa.eu), the official EU portal, remains the recommended primary source — paste an official EURES/national-PES feed URL and enable it. All network sources ship disabled so nothing is fetched until you opt in; every fetch still honours robots.txt and per-domain rate limits.

Import jobs: don't want to configure a feed yet? Click **Import jobs…** on the Jobs tab to load listings from a **CSV or JSON** file, or grab a **CSV template** / **JSON template** (the buttons next to it) to see the exact columns. Column/key names are matched flexibly (e.g. `Job Title`, `job_title`, `position` all map to the title); each row is de-duplicated and enriched (salary + competencies) just like collected jobs.

Job-alert email: turn on **Job-alert email ingestion** in Settings and give RJH your mailbox details (IMAP or POP3; use an app password). It connects read-only, finds the postings in each alert, unwraps the tracking links to the real URLs, and never stores your address. Prefer not to hand over credentials? Save an alert email and drag it in with **Import .eml…** on the Jobs tab — no mailbox access at all. An optional sender allowlist limits which addresses are parsed.

Careers-page crawler: add entries to the **Careers-page crawler** JSON in Settings — a `url`, a `max_depth`, a `max_pages`, and `enabled: true`. RJH walks that site breadth-first, stays on the same host, honours robots.txt (fail-closed) and the rate limit, and stages every page that looks like a posting. Prefer an official feed or an emailed alert where one exists; crawl only public pages you're allowed to.

Automation: the **Automation — background scheduler** card runs the whole pipeline on an interval, unattended. Optionally it auto-drafts a resume and cover letter for the top new matches (needs Ollama). It only ever *stages* work for your review — it never submits. Use **Run a pass now** to trigger one immediately.

AI analysis: with AI tools on, each job row gains an **Analyze** button — local-LLM keyword/entity extraction, a translation of the posting, and an honest fit summary grounded only in your resume. Results appear in the Documents tab and are saved locally.

OpenDocument export: in the Documents tab, **Download .odt** compiles the current resume or cover-letter text (including your edits) into a formatted OpenDocument file — headings, bullet lists and a contact header — using a built-in stdlib compiler, so it needs no extra packages and works offline. The same compiler produces the file that the pre-fill step attaches when you haven't set a curated résumé/cover-letter path in your Profile.

AI document tools: off-by-default-friendly. The **Enable AI document tools** checkbox in Settings turns the optional Ollama layer on or off. With it off, the Documents tab and the per-job Generate buttons disappear and the rest of the app is unaffected. After enabling, use **Settings → Setup** to install Ollama and pull a model.

Browsing jobs: click any column header to sort (click again to reverse); the search box matches every field including salary and competencies; **Re-score all** recomputes score, salary and competencies for stored jobs after you change your profile.

Pre-fill site rules: explicit CSS-selector rules per site, applied first; a multilingual generic fallback (English, French, German, Dutch, Nordic) fills the rest by reading each field's name, id, placeholder, aria-label, autocomplete, and label text. Adding a site needs no code change.

Profile: fill in your master profile, keywords (which drive scoring), and optional absolute paths to a curated resume/cover-letter PDF, which are attached during pre-fill in preference to the generated text. You can also **Import resume…** from a PDF, ODT, TXT, or Markdown file — the text is extracted locally into the master-resume box for you to review before saving.

## Roadmap

- More worked source adapters (additional national-PES APIs; an official EURES feed once a stable public URL is settled on).
- A selector-capture helper to build site rules faster.
- Optional semantic matching and richer scoring.
- Per-sender parsing rules for the email ingester (board-specific layouts).
- Re-using the crawler's full-page extraction to enrich feed/email postings with the complete description.

Recently shipped: job-alert email ingestion (IMAP/POP3/`.eml`), a careers-page crawler, a hardened single fetch pathway (SSRF guard, fail-closed robots, `Crawl-delay`, conditional GET), local-LLM translation/keyword-extraction/fit summaries, and a background scheduler.

## Diagnostics

Something not behaving? **Settings → Diagnostics** downloads a redacted `.zip` snapshot — environment and build fingerprint, your settings with secrets removed, database counts, scheduler and discovery state, and the recent audit log (with errors called out). Your resume, profile text, job descriptions and passwords are never included, so the bundle is safe to attach to a bug report or hand back for help debugging.

## Contributing

Issues and pull requests welcome. Two hard rules: keep the human-in-the-loop submit guarantee, and respect robots.txt and rate limits. Anything that automates submission or evades anti-bot measures will be declined.

The core is pure standard library, so there is nothing to install to develop or test. The offline test suite (`tests/`) and a byte-compile run on every push/PR via GitHub Actions:

```sh
python -m py_compile rjh.py
python -m unittest discover -s tests -p "test_*.py"
```

## Disclaimer

Provided as-is under the GNU General Public License v3.0, with no warranty. You are responsible for complying with the Terms of Service of any site you point it at and with applicable data-protection law.
