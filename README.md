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

1. Collects job postings from config-driven source adapters (RSS/Atom feeds and JSON job APIs), respecting robots.txt and per-domain rate limits — or import a CSV/JSON list of jobs directly when you have no feed configured.
2. Stores them in a searchable local SQLite database with duplicate detection (URL canonicalization plus content hashing).
3. Enriches each posting locally: scores it against your master profile, parses a salary range from the text, and extracts competency tags — no AI required.
4. Lets you full-text search across title, company, location, country, salary, competencies and description, and sort by any column (score, title, company, location, salary, competencies, status) just by clicking the header.
5. *(Optional add-on)* Generates a tailored resume and cover letter per role using your local Ollama model, grounded only in your real master resume.
6. Pre-fills the application form in a real browser using a per-site field-mapping system, then stops. You review, edit, and submit yourself.
7. Logs every action to the database and to a dated Markdown audit trail.

The scraper, database, search, sort, scoring, profile, resume import, export and browser pre-fill form the **core** and run with no AI at all. The LLM document tools are an **optional add-on**, toggled in Settings; everything else keeps working when it is off.

Tuned for Western and Northern Europe. EURES (https://eures.europa.eu), the official EU job-mobility portal, is the recommended primary source; national public-employment-service feeds and company career-page feeds plug in the same way.

## Ethics (built into the code)

- A human always submits. RJH never clicks a submit button.
- Respects robots.txt for every domain before fetching.
- Rate-limits itself per domain.
- Prefers official feeds and APIs and public career pages over scraping.
- Stays local: document generation runs on your own Ollama instance; your resume never leaves your machine.

Auto-submitting to sites such as LinkedIn or Indeed violates their Terms of Service and risks account bans. RJH deliberately does not do that.

## Install

One line — clones the repo, creates a virtualenv, installs the core dependencies, then **starts RJH and opens it in your browser** automatically:

```
curl -fsSL https://raw.githubusercontent.com/ideotion/RJH/main/install.sh | sh
```

The installer is a short, readable, GPLv3 shell script ([install.sh](install.sh)). Set `RJH_DIR=/path` to choose where it goes (default `~/rjh`), or `RJH_NO_START=1` to install without launching. To run it again later: `cd ~/rjh && ./venv/bin/python rjh.py`.

Or do it by hand:

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins three required packages (fastapi, uvicorn, requests). The rest are optional and only loaded when the matching feature is used:

- `playwright` — the browser pre-fill step. After `pip install playwright`, fetch an engine with `playwright install firefox` (Firefox is the default; `chromium` and `webkit` also work, selectable in Settings).
- `pypdf` / `odfpy` — importing a resume from a PDF or ODT file (parsed locally).

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

AI document tools: off-by-default-friendly. The **Enable AI document tools** checkbox in Settings turns the optional Ollama layer on or off. With it off, the Documents tab and the per-job Generate buttons disappear and the rest of the app is unaffected. After enabling, use **Settings → Setup** to install Ollama and pull a model.

Browsing jobs: click any column header to sort (click again to reverse); the search box matches every field including salary and competencies; **Re-score all** recomputes score, salary and competencies for stored jobs after you change your profile.

Pre-fill site rules: explicit CSS-selector rules per site, applied first; a multilingual generic fallback (English, French, German, Dutch, Nordic) fills the rest by reading each field's name, id, placeholder, aria-label, autocomplete, and label text. Adding a site needs no code change.

Profile: fill in your master profile, keywords (which drive scoring), and optional absolute paths to a curated resume/cover-letter PDF, which are attached during pre-fill in preference to the generated text. You can also **Import resume…** from a PDF, ODT, TXT, or Markdown file — the text is extracted locally into the master-resume box for you to review before saving.

## Roadmap

- More worked source adapters (additional national-PES APIs; an official EURES feed once a stable public URL is settled on).
- A selector-capture helper to build site rules faster.
- Optional semantic matching and richer scoring.
- Local-LLM translation and summarization of postings.

## Contributing

Issues and pull requests welcome. Two hard rules: keep the human-in-the-loop submit guarantee, and respect robots.txt and rate limits. Anything that automates submission or evades anti-bot measures will be declined.

## Disclaimer

Provided as-is under the GNU General Public License v3.0, with no warranty. You are responsible for complying with the Terms of Service of any site you point it at and with applicable data-protection law.
