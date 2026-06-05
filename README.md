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

1. Collects job postings from config-driven source adapters (any RSS or Atom feed), respecting robots.txt and per-domain rate limits.
2. Stores them in a searchable local SQLite database with duplicate detection (URL canonicalization plus content hashing).
3. Scores each posting against your master profile so the best fits surface first.
4. Generates a tailored resume and cover letter per role using your local Ollama model, grounded only in your real master resume.
5. Pre-fills the application form in a real browser using a per-site field-mapping system, then stops. You review, edit, and submit yourself.
6. Logs every action to the database and to a dated Markdown audit trail.

Tuned for Western and Northern Europe. EURES (https://eures.europa.eu), the official EU job-mobility portal, is the recommended primary source; national public-employment-service feeds and company career-page feeds plug in the same way.

## Ethics (built into the code)

- A human always submits. RJH never clicks a submit button.
- Respects robots.txt for every domain before fetching.
- Rate-limits itself per domain.
- Prefers official feeds and APIs and public career pages over scraping.
- Stays local: document generation runs on your own Ollama instance; your resume never leaves your machine.

Auto-submitting to sites such as LinkedIn or Indeed violates their Terms of Service and risks account bans. RJH deliberately does not do that.

## Install

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# for the pre-fill step:
playwright install chromium
```

`requirements.txt` pins three required packages (fastapi, uvicorn, requests). The rest are optional and only loaded when the matching feature is used:

- `playwright` — the browser pre-fill step.
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

Open the printed URL (default http://127.0.0.1:8765). Your database and audit trail live in ./rjh_data/.

## Configure

Everything is editable from the Settings tab and persisted to rjh_data/config.json.

Sources: add any RSS/Atom feed; set enabled true and a real URL.

Pre-fill site rules: explicit CSS-selector rules per site, applied first; a multilingual generic fallback (English, French, German, Dutch, Nordic) fills the rest by reading each field's name, id, placeholder, aria-label, autocomplete, and label text. Adding a site needs no code change.

Profile: fill in your master profile, keywords (which drive scoring), and optional absolute paths to a curated resume/cover-letter PDF, which are attached during pre-fill in preference to the generated text. You can also **Import resume…** from a PDF, ODT, TXT, or Markdown file — the text is extracted locally into the master-resume box for you to review before saving.

## Roadmap

- More worked source adapters (EURES, national PES feeds, generic JSON API).
- A selector-capture helper to build site rules faster.
- Optional semantic matching and richer scoring.
- Local-LLM translation and summarization of postings.

## Contributing

Issues and pull requests welcome. Two hard rules: keep the human-in-the-loop submit guarantee, and respect robots.txt and rate limits. Anything that automates submission or evades anti-bot measures will be declined.

## Disclaimer

Provided as-is under the GNU General Public License v3.0, with no warranty. You are responsible for complying with the Terms of Service of any site you point it at and with applicable data-protection law.
