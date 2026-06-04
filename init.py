"""
ReverseJobHunter - an ethical, open-source, local-first job application copilot.

WHAT IT DOES
  1. Collects job postings from config-driven source adapters (RSS/JSON feeds),
     respecting robots.txt and per-domain rate limits.
  2. Stores them in a searchable local SQLite database with duplicate detection
     (URL canonicalization + content hashing).
  3. Scores each posting against your master profile so the best fits surface first.
  4. Uses your LOCAL Ollama instance to tailor a resume and cover letter per job.
     Nothing leaves your machine.
  5. Optionally pre-fills the application form in a browser (Playwright) and then
     STOPS. You review, edit, and click submit yourself. That click is the only
     thing that ever sends anything.
  6. Logs every action to the database and to a dated markdown audit trail.

GEOGRAPHY
  Tuned for Western and Northern Europe. EURES (the official EU job mobility
  portal) is the recommended primary source: it is built for cross-border hiring
  and is feed-friendly. Add national public-employment-service feeds and company
  career-page feeds the same way (see SOURCES in the config).

ETHICS (non-negotiable, baked in)
  - Respects robots.txt for every domain before fetching.
  - Rate-limits itself per domain.
  - Prefers official feeds/APIs and public career pages over scraping.
  - Never auto-submits. A human gives the final go on every application.
  - Auto-submitting to sites like LinkedIn/Indeed violates their Terms of Service
    and risks account bans; this tool deliberately does not do that.

INSTALL
  python3 -m venv venv && source venv/bin/activate
  pip install fastapi uvicorn requests
  # optional, only for the form pre-fill assistant:
  pip install playwright && playwright install chromium
  # local LLM:
  # install Ollama from https://ollama.com, then:  ollama pull mistral

RUN
  python3 openjobpilot.py
  # then open the printed URL (default http://127.0.0.1:8765) in your browser.
"""

import os
import re
import csv
import json
import time
import html
import hashlib
import sqlite3
import threading
import datetime as dt
from io import StringIO
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib import robotparser
from xml.etree import ElementTree as ET

import requests

try:
    from fastapi import FastAPI, Request, Response, Query
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    import uvicorn
except ImportError:
    raise SystemExit("Missing dependencies. Run:  pip install fastapi uvicorn requests")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DATA_DIR = os.path.join(os.getcwd(), "openjobpilot_data")
DB_PATH = os.path.join(DATA_DIR, "openjobpilot.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
USER_AGENT = "OpenJobPilot/0.1 (+https://github.com/; ethical open-source job copilot)"

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "mistral",
    "output_language": "auto",          # auto = match the posting's language
    "rate_limit_seconds": 5,            # minimum seconds between hits to one domain
    "request_timeout": 20,
    # Country preference for Western & Northern Europe. ISO-2 codes, ordered.
    "preferred_countries": ["NL", "DE", "SE", "DK", "NO", "FI", "BE",
                            "LU", "AT", "CH", "IE", "IS", "GB", "FR"],
    # Config-driven sources. type "rss" works for any RSS/Atom feed.
    # Replace/extend with the official feeds you trust. The demo source lets the
    # app run end-to-end offline so you can try the workflow immediately.
    "sources": [
        {
            "name": "DEMO (offline sample)",
            "type": "demo",
            "enabled": True,
            "default_country": "EU"
        },
        {
            "name": "EURES (official EU portal)",
            "type": "rss",
            "enabled": False,
            "url": "PASTE_AN_OFFICIAL_EURES_OR_PES_FEED_URL_HERE",
            "default_country": "EU"
        }
    ]
}


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_config():
    ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # backfill any missing keys
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    ensure_dirs()
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                url TEXT,
                canonical_url TEXT,
                url_hash TEXT UNIQUE,
                content_hash TEXT,
                title TEXT,
                company TEXT,
                location TEXT,
                country TEXT,
                description TEXT,
                posted_at TEXT,
                fetched_at TEXT,
                score INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new'
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                kind TEXT,
                content TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                action TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
            CREATE INDEX IF NOT EXISTS idx_docs_job ON documents(job_id);
            """
        )
        row = conn.execute("SELECT 1 FROM profile WHERE id = 1").fetchone()
        if not row:
            default_profile = {
                "name": "Your Name",
                "headline": "",
                "location": "",
                "email": "",
                "phone": "",
                "keywords": [],
                "resume": "Paste your master resume here, in plain text."
            }
            conn.execute("INSERT INTO profile (id, data) VALUES (1, ?)",
                        (json.dumps(default_profile),))


def audit(action, detail=""):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO audit (ts, action, detail) VALUES (?,?,?)",
                    (ts, action, detail))
    # mirror to a dated markdown trail
    fname = dt.datetime.now().strftime("%Y%m%d") + "_OpenJobPilot_AUDIT_TRAIL.md"
    path = os.path.join(DATA_DIR, fname)
    line = "- {} | {} | {}\n".format(ts, action, detail.replace("\n", " "))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def get_profile():
    with db() as conn:
        row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    return json.loads(row["data"]) if row else {}


def set_profile(data):
    with _db_lock, db() as conn:
        conn.execute("UPDATE profile SET data = ? WHERE id = 1",
                    (json.dumps(data),))
    audit("profile_updated", data.get("name", ""))


# --------------------------------------------------------------------------- #
# Ethical fetching: robots.txt + per-domain rate limiting + dedup helpers
# --------------------------------------------------------------------------- #

_robots_cache = {}
_last_hit = {}
_rl_lock = threading.Lock()

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                  "utm_content", "gclid", "fbclid", "ref", "source"}


def canonicalize_url(url):
    try:
        p = urlparse(url)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path.rstrip("/") or "/"
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
             if k.lower() not in TRACKING_PARAMS]
        q.sort()
        query = urlencode(q)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url


def url_hash(url):
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def content_hash(title, company, description):
    norm = re.sub(r"\s+", " ", " ".join([title or "", company or "",
                                          (description or "")[:2000]])).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def robots_allows(url, cfg):
    p = urlparse(url)
    if not p.netloc:
        return False
    base = "{}://{}".format(p.scheme or "https", p.netloc)
    rp = _robots_cache.get(base)
    if rp is None:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            # if robots cannot be read, be conservative and allow only
            # the explicit feed the user configured (treated as opt-in)
            rp = None
        _robots_cache[base] = rp
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


def rate_limited_get(url, cfg):
    domain = urlparse(url).netloc
    interval = float(cfg.get("rate_limit_seconds", 5))
    with _rl_lock:
        last = _last_hit.get(domain, 0)
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _last_hit[domain] = time.time()
    return requests.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=cfg.get("request_timeout", 20))


# --------------------------------------------------------------------------- #
# Source adapters
# --------------------------------------------------------------------------- #

def parse_feed(xml_text):
    """Minimal RSS + Atom parser. Returns a list of normalized dicts."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    def text(el, *tags):
        for t in tags:
            found = el.find(t)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
            # namespaced fallback
            for child in el:
                if child.tag.split("}")[-1] == t and (child.text or "").strip():
                    return child.text.strip()
        return ""

    # RSS
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag == "item":
            link = text(item, "link", "guid")
            items.append({
                "title": text(item, "title"),
                "url": link,
                "description": text(item, "description", "summary", "content"),
                "company": text(item, "author", "creator", "source"),
                "posted_at": text(item, "pubDate", "date", "published"),
            })
        elif tag == "entry":  # Atom
            link = ""
            for l in item.findall("{*}link"):
                link = l.attrib.get("href", link)
            items.append({
                "title": text(item, "title"),
                "url": link,
                "description": text(item, "summary", "content"),
                "company": text(item, "author", "name"),
                "posted_at": text(item, "updated", "published"),
            })
    return items


DEMO_JOBS = [
    {"title": "Chief Operating Officer, MedTech Manufacturing",
     "company": "Northern Biolabs", "location": "Amsterdam, NL", "country": "NL",
     "url": "https://example.org/jobs/coo-medtech-amsterdam",
     "description": "Lead a 30+ person site producing implantable medical devices "
                    "under ISO 13485 and GMP. Own QMS, CAPA, IQ/OQ/PQ qualification, "
                    "cleanroom operations and ERP. Fluent English required."},
    {"title": "Head of Regulatory Affairs, Pediatric Pharma",
     "company": "Helsinki Therapeutics", "location": "Helsinki, FI", "country": "FI",
     "url": "https://example.org/jobs/head-regaffairs-helsinki",
     "description": "Own EMA CTD filings and scientific-opinion procedures for "
                    "rare pediatric diseases. Phase 1/2 clinical coordination."},
    {"title": "Director of Operations, Life Sciences CRO",
     "company": "Copenhagen Bioanalytics", "location": "Copenhagen, DK", "country": "DK",
     "url": "https://example.org/jobs/dir-ops-copenhagen",
     "description": "Run a GLP/ICH M10 bioanalytical lab. Method validation, "
                    "study director supervision, client relations, sample stock."},
]


def collect_from_source(src, cfg):
    """Returns a list of normalized job dicts for one source."""
    out = []
    stype = src.get("type")
    if not src.get("enabled"):
        return out

    if stype == "demo":
        for j in DEMO_JOBS:
            d = dict(j)
            d["source"] = src["name"]
            d["posted_at"] = dt.datetime.now().isoformat(timespec="seconds")
            out.append(d)
        return out

    if stype == "rss":
        url = src.get("url", "")
        if not url or url.startswith("PASTE_"):
            return out
        if not robots_allows(url, cfg):
            audit("robots_blocked", url)
            return out
        try:
            resp = rate_limited_get(url, cfg)
            if resp.status_code != 200:
                audit("fetch_failed", "{} -> {}".format(url, resp.status_code))
                return out
            for entry in parse_feed(resp.text):
                if not entry.get("url"):
                    continue
                out.append({
                    "source": src["name"],
                    "title": html.unescape(entry.get("title", "")),
                    "company": html.unescape(entry.get("company", "")),
                    "location": "",
                    "country": src.get("default_country", ""),
                    "url": entry.get("url"),
                    "description": html.unescape(
                        re.sub(r"<[^>]+>", " ", entry.get("description", ""))),
                    "posted_at": entry.get("posted_at", ""),
                })
        except Exception as e:
            audit("fetch_error", "{}: {}".format(url, e))
        return out

    return out


def collect_all(cfg):
    added, skipped = 0, 0
    profile = get_profile()
    for src in cfg.get("sources", []):
        for j in collect_from_source(src, cfg):
            uh = url_hash(j["url"])
            ch = content_hash(j.get("title"), j.get("company"), j.get("description"))
            with _db_lock, db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM jobs WHERE url_hash = ? OR content_hash = ?",
                    (uh, ch)).fetchone()
                if exists:
                    skipped += 1
                    continue
                score = score_job(j, profile, cfg)
                conn.execute(
                    """INSERT INTO jobs (source,url,canonical_url,url_hash,content_hash,
                       title,company,location,country,description,posted_at,fetched_at,
                       score,status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                    (j.get("source"), j.get("url"), canonicalize_url(j["url"]), uh, ch,
                     j.get("title"), j.get("company"), j.get("location"),
                     j.get("country"), j.get("description"), j.get("posted_at"),
                     dt.datetime.now().isoformat(timespec="seconds"), score))
                added += 1
    audit("collect", "added={} skipped(dupes)={}".format(added, skipped))
    return {"added": added, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Matching / scoring
# --------------------------------------------------------------------------- #

_word_re = re.compile(r"[a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ\-]{2,}")


def tokens(text):
    return set(w.lower() for w in _word_re.findall(text or ""))


def score_job(job, profile, cfg):
    kws = set(k.lower() for k in profile.get("keywords", []) if k)
    kws |= tokens(profile.get("headline", ""))
    if not kws:
        base = 50
    else:
        job_tokens = tokens(job.get("title", "")) | tokens(job.get("description", ""))
        overlap = len(kws & job_tokens)
        base = min(90, int(100 * overlap / max(6, len(kws))))
    # title keyword hits weigh extra
    title_hits = len(kws & tokens(job.get("title", "")))
    base = min(100, base + title_hits * 4)
    # country preference bonus
    prefs = [c.upper() for c in cfg.get("preferred_countries", [])]
    country = (job.get("country") or "").upper()
    if country and country in prefs:
        rank = prefs.index(country)
        base = min(100, base + max(0, 8 - rank))
    return int(base)


# --------------------------------------------------------------------------- #
# Ollama (local LLM) document generation
# --------------------------------------------------------------------------- #

def ollama_status(cfg):
    try:
        r = requests.get(cfg["ollama_url"] + "/api/tags", timeout=4)
        if r.status_code == 200:
            models = [m.get("name") for m in r.json().get("models", [])]
            return {"up": True, "models": models}
    except Exception:
        pass
    return {"up": False, "models": []}


def ollama_chat(cfg, system, user):
    payload = {
        "model": cfg["ollama_model"],
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    r = requests.post(cfg["ollama_url"] + "/api/chat", json=payload,
                      timeout=300)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "").strip()


def generate_documents(job, profile, cfg):
    lang = cfg.get("output_language", "auto")
    lang_instr = ("Write in the same language as the job posting."
                  if lang == "auto" else "Write in {}.".format(lang))
    system = ("You are an expert career writer. You produce honest, specific, "
              "concise application material grounded ONLY in the candidate's real "
              "master resume. Never invent experience, employers, dates, or skills. "
              + lang_instr)

    base = ("CANDIDATE MASTER RESUME:\n{resume}\n\n"
            "CANDIDATE HEADLINE: {headline}\n\n"
            "JOB TITLE: {title}\nCOMPANY: {company}\nLOCATION: {location}\n"
            "JOB DESCRIPTION:\n{desc}\n").format(
                resume=profile.get("resume", ""),
                headline=profile.get("headline", ""),
                title=job["title"], company=job["company"],
                location=job.get("location", ""),
                desc=job.get("description", ""))

    resume_prompt = (base + "\nTASK: Produce a tailored, ATS-friendly resume in "
                     "plain text targeted at this role. Reorder and emphasize the "
                     "candidate's real experience to match the job. Do not fabricate.")
    cover_prompt = (base + "\nTASK: Write a focused cover letter (max ~300 words) "
                    "in plain text for this role. Specific, sincere, no clichés. "
                    "Use only real facts from the master resume.")

    resume = ollama_chat(cfg, system, resume_prompt)
    cover = ollama_chat(cfg, system, cover_prompt)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM documents WHERE job_id = ?", (job["id"],))
        conn.execute("INSERT INTO documents (job_id,kind,content,created_at) VALUES (?,?,?,?)",
                    (job["id"], "resume", resume, now))
        conn.execute("INSERT INTO documents (job_id,kind,content,created_at) VALUES (?,?,?,?)",
                    (job["id"], "cover_letter", cover, now))
        conn.execute("UPDATE jobs SET status = 'generated' WHERE id = ? AND status IN ('new','shortlisted')",
                    (job["id"],))
    audit("generate", "job_id={} title={}".format(job["id"], job["title"]))
    return {"resume": resume, "cover_letter": cover}


# --------------------------------------------------------------------------- #
# Application assistant (Playwright pre-fill, optional, human-confirmed submit)
# --------------------------------------------------------------------------- #

def prefill_application(job_url):
    """Opens the posting in a visible browser, attempts a best-effort pre-fill
    of obvious text fields, then HANDS CONTROL TO THE HUMAN. It never clicks a
    submit button. Requires:  pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False,
                "msg": "Playwright not installed. Run: pip install playwright "
                       "&& playwright install chromium"}
    profile = get_profile()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(job_url, wait_until="domcontentloaded")
            # Best-effort pre-fill by common field names. Sites differ wildly;
            # treat this as a head start, not a finished application.
            field_map = {
                ("name", "full_name", "fullname"): profile.get("name", ""),
                ("email", "e-mail"): profile.get("email", ""),
                ("phone", "tel", "telephone"): profile.get("phone", ""),
            }
            for keys, value in field_map.items():
                if not value:
                    continue
                for k in keys:
                    try:
                        page.fill("input[name*='{}' i]".format(k), value, timeout=1000)
                        break
                    except Exception:
                        continue
            audit("prefill", job_url)
            # Park here. The human reviews, finishes, and submits manually.
            input("Form pre-filled. Review and submit MANUALLY in the browser, "
                  "then press Enter here to close...")
            browser.close()
        return {"ok": True, "msg": "Pre-fill session finished. Submit was left to you."}
    except Exception as e:
        return {"ok": False, "msg": "Pre-fill error: {}".format(e)}


# --------------------------------------------------------------------------- #
# Web GUI (served to your real browser)
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>OpenJobPilot</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--line:#272e3a;--txt:#e6edf3;--mut:#8b949e;
        --acc:#3b82f6;--good:#3fb950;--warn:#d29922;--bad:#f85149;}
  *{box-sizing:border-box;font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}
  body{margin:0;background:var(--bg);color:var(--txt);font-size:14px}
  header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
  header h1{font-size:18px;margin:0}
  header .pill{font-size:11px;color:var(--mut);border:1px solid var(--line);padding:3px 8px;border-radius:20px}
  nav{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
  nav button{background:transparent;color:var(--mut);border:1px solid transparent;padding:7px 12px;border-radius:7px;cursor:pointer}
  nav button.active{color:var(--txt);background:var(--card);border-color:var(--line)}
  main{padding:20px;max-width:1100px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  button.btn{background:var(--acc);color:#fff;border:0;padding:8px 14px;border-radius:7px;cursor:pointer}
  button.btn.sec{background:transparent;color:var(--txt);border:1px solid var(--line)}
  button.btn:disabled{opacity:.5;cursor:not-allowed}
  input,select,textarea{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:8px;font-size:13px}
  textarea{width:100%;min-height:120px;font-family:ui-monospace,Menlo,Consolas,monospace}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px}
  .score{font-weight:700}
  .tag{font-size:11px;padding:2px 7px;border-radius:12px;border:1px solid var(--line);color:var(--mut)}
  .muted{color:var(--mut)}
  a{color:var(--acc)}
  .hide{display:none}
  label{display:block;margin:8px 0 3px;color:var(--mut);font-size:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .toast{position:fixed;bottom:18px;right:18px;background:var(--card);border:1px solid var(--line);padding:10px 14px;border-radius:8px}
</style></head>
<body>
<header>
  <h1>OpenJobPilot</h1>
  <span class="pill" id="ollamaPill">Ollama: checking...</span>
  <nav>
    <button data-tab="jobs" class="active">Jobs</button>
    <button data-tab="docs">Documents</button>
    <button data-tab="profile">Profile</button>
    <button data-tab="settings">Settings</button>
    <button data-tab="audit">Audit</button>
  </nav>
</header>
<main>

<section id="tab-jobs">
  <div class="card">
    <div class="row">
      <button class="btn" id="collectBtn">Collect jobs</button>
      <input id="q" placeholder="Search title, company, text..." style="flex:1;min-width:180px"/>
      <select id="statusFilter">
        <option value="">All statuses</option>
        <option value="new">New</option>
        <option value="shortlisted">Shortlisted</option>
        <option value="generated">Generated</option>
        <option value="applied">Applied</option>
        <option value="archived">Archived</option>
      </select>
      <input id="minScore" type="number" min="0" max="100" value="0" style="width:90px" title="Min score"/>
      <button class="btn sec" id="searchBtn">Filter</button>
      <a class="btn sec" href="/api/export?format=csv" style="text-decoration:none">Export CSV</a>
      <a class="btn sec" href="/api/export?format=json" style="text-decoration:none">Export JSON</a>
    </div>
  </div>
  <div class="card">
    <table><thead><tr>
      <th>Score</th><th>Title</th><th>Company</th><th>Where</th><th>Status</th><th>Actions</th>
    </tr></thead><tbody id="jobsBody"></tbody></table>
  </div>
</section>

<section id="tab-docs" class="hide">
  <div class="card" id="docsEmpty"><span class="muted">Select a job and click Generate to produce a tailored resume and cover letter here.</span></div>
  <div class="card hide" id="docsPanel">
    <div class="row"><h3 id="docsTitle" style="margin:0"></h3>
      <button class="btn" id="regenBtn" style="margin-left:auto">Regenerate</button></div>
    <label>Tailored resume</label>
    <textarea id="resumeBox"></textarea>
    <div class="row"><button class="btn sec" onclick="copyBox('resumeBox')">Copy resume</button></div>
    <label>Cover letter</label>
    <textarea id="coverBox"></textarea>
    <div class="row"><button class="btn sec" onclick="copyBox('coverBox')">Copy cover letter</button>
      <button class="btn" id="prefillBtn" style="margin-left:auto">Pre-fill application (you submit)</button></div>
  </div>
</section>

<section id="tab-profile" class="hide">
  <div class="card">
    <div class="grid2">
      <div><label>Name</label><input id="pName" style="width:100%"/></div>
      <div><label>Headline</label><input id="pHeadline" style="width:100%"/></div>
      <div><label>Email</label><input id="pEmail" style="width:100%"/></div>
      <div><label>Phone</label><input id="pPhone" style="width:100%"/></div>
      <div><label>Location</label><input id="pLocation" style="width:100%"/></div>
      <div><label>Keywords (comma separated)</label><input id="pKeywords" style="width:100%"/></div>
    </div>
    <label>Master resume (plain text). Everything is generated only from this.</label>
    <textarea id="pResume" style="min-height:240px"></textarea>
    <div class="row"><button class="btn" id="saveProfile">Save profile</button></div>
  </div>
</section>

<section id="tab-settings" class="hide">
  <div class="card">
    <div class="grid2">
      <div><label>Ollama URL</label><input id="cOllamaUrl" style="width:100%"/></div>
      <div><label>Ollama model</label><input id="cOllamaModel" style="width:100%"/></div>
      <div><label>Output language (auto = match posting)</label><input id="cLang" style="width:100%"/></div>
      <div><label>Rate limit seconds per domain</label><input id="cRate" type="number" style="width:100%"/></div>
    </div>
    <label>Preferred countries (ISO-2, comma separated, ordered)</label>
    <input id="cCountries" style="width:100%"/>
    <label>Sources (JSON). type "rss" = any RSS/Atom feed; set enabled true and a real URL.</label>
    <textarea id="cSources" style="min-height:200px"></textarea>
    <div class="row"><button class="btn" id="saveSettings">Save settings</button>
      <span class="muted">Restart the app to change host/port.</span></div>
  </div>
</section>

<section id="tab-audit" class="hide">
  <div class="card"><table><thead><tr><th>Time</th><th>Action</th><th>Detail</th></tr></thead>
    <tbody id="auditBody"></tbody></table></div>
</section>

</main>
<div id="toast" class="toast hide"></div>
<script>
let currentJob = null;
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.remove('hide');setTimeout(()=>t.classList.add('hide'),2600);}
function copyBox(id){const el=document.getElementById(id);el.select();document.execCommand('copy');toast('Copied');}
async function api(path,opts){const r=await fetch(path,opts);if(!r.ok)throw new Error(await r.text());const ct=r.headers.get('content-type')||'';return ct.includes('json')?r.json():r.text();}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('main > section').forEach(s=>s.classList.add('hide'));
  document.getElementById('tab-'+b.dataset.tab).classList.remove('hide');
  if(b.dataset.tab==='audit')loadAudit();
});

async function refreshOllama(){
  try{const s=await api('/api/ollama');
    document.getElementById('ollamaPill').textContent = s.up
      ? 'Ollama: up ('+(s.models.join(', ')||'no models')+')' : 'Ollama: offline';
  }catch(e){}
}

function statusTag(s){return '<span class="tag">'+s+'</span>';}

async function loadJobs(){
  const q=encodeURIComponent(document.getElementById('q').value);
  const st=document.getElementById('statusFilter').value;
  const ms=document.getElementById('minScore').value||0;
  const jobs=await api(`/api/jobs?q=${q}&status=${st}&min_score=${ms}`);
  const body=document.getElementById('jobsBody');body.innerHTML='';
  if(!jobs.length){body.innerHTML='<tr><td colspan="6" class="muted">No jobs yet. Click Collect jobs.</td></tr>';return;}
  jobs.forEach(j=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="score">${j.score}</td>
      <td><a href="${j.url}" target="_blank" rel="noopener">${j.title||''}</a><div class="muted" style="font-size:11px">${j.source||''}</div></td>
      <td>${j.company||''}</td>
      <td>${(j.location||'')||(j.country||'')}</td>
      <td>${statusTag(j.status)}</td>
      <td class="row">
        <button class="btn" data-gen="${j.id}">Generate</button>
        <button class="btn sec" data-short="${j.id}">Shortlist</button>
        <button class="btn sec" data-arch="${j.id}">Archive</button>
      </td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll('[data-gen]').forEach(b=>b.onclick=()=>generate(b.dataset.gen));
  body.querySelectorAll('[data-short]').forEach(b=>b.onclick=()=>setStatus(b.dataset.short,'shortlisted'));
  body.querySelectorAll('[data-arch]').forEach(b=>b.onclick=()=>setStatus(b.dataset.arch,'archived'));
}

async function setStatus(id,status){await api(`/api/jobs/${id}/status`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({status})});toast('Updated');loadJobs();}

async function generate(id){
  toast('Generating locally with Ollama, please wait...');
  try{
    const r=await api(`/api/generate/${id}`,{method:'POST'});
    openDocs(r.job,r.resume,r.cover_letter);
  }catch(e){toast('Generation failed. Is Ollama running and the model pulled?');}
}

function openDocs(job,resume,cover){
  currentJob=job;
  document.querySelector('nav button[data-tab="docs"]').click();
  document.getElementById('docsEmpty').classList.add('hide');
  document.getElementById('docsPanel').classList.remove('hide');
  document.getElementById('docsTitle').textContent=job.title+' — '+(job.company||'');
  document.getElementById('resumeBox').value=resume;
  document.getElementById('coverBox').value=cover;
}

document.getElementById('regenBtn').onclick=()=>{if(currentJob)generate(currentJob.id);};
document.getElementById('prefillBtn').onclick=async()=>{
  if(!currentJob)return;
  if(!confirm('This opens a real browser and pre-fills what it can. It will NOT submit. You review and submit yourself. Continue?'))return;
  toast('Launching browser... watch your desktop.');
  try{const r=await api(`/api/prefill/${currentJob.id}`,{method:'POST'});toast(r.msg);}
  catch(e){toast('Pre-fill failed: '+e.message);}
};

document.getElementById('collectBtn').onclick=async()=>{
  toast('Collecting (robots-aware, rate-limited)...');
  try{const r=await api('/api/collect',{method:'POST'});toast(`Added ${r.added}, skipped ${r.skipped} duplicates`);loadJobs();}
  catch(e){toast('Collect failed');}
};
document.getElementById('searchBtn').onclick=loadJobs;
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')loadJobs();});

async function loadProfile(){
  const p=await api('/api/profile');
  pName.value=p.name||'';pHeadline.value=p.headline||'';pEmail.value=p.email||'';
  pPhone.value=p.phone||'';pLocation.value=p.location||'';
  pKeywords.value=(p.keywords||[]).join(', ');pResume.value=p.resume||'';
}
document.getElementById('saveProfile').onclick=async()=>{
  const body={name:pName.value,headline:pHeadline.value,email:pEmail.value,
    phone:pPhone.value,location:pLocation.value,
    keywords:pKeywords.value.split(',').map(s=>s.trim()).filter(Boolean),
    resume:pResume.value};
  await api('/api/profile',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  toast('Profile saved. New jobs will be scored against it.');
};

async function loadSettings(){
  const c=await api('/api/config');
  cOllamaUrl.value=c.ollama_url;cOllamaModel.value=c.ollama_model;
  cLang.value=c.output_language;cRate.value=c.rate_limit_seconds;
  cCountries.value=(c.preferred_countries||[]).join(', ');
  cSources.value=JSON.stringify(c.sources,null,2);
}
document.getElementById('saveSettings').onclick=async()=>{
  let sources;try{sources=JSON.parse(cSources.value);}catch(e){return toast('Sources JSON is invalid');}
  const body={ollama_url:cOllamaUrl.value,ollama_model:cOllamaModel.value,
    output_language:cLang.value,rate_limit_seconds:Number(cRate.value),
    preferred_countries:cCountries.value.split(',').map(s=>s.trim()).filter(Boolean),
    sources};
  await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  toast('Settings saved');refreshOllama();
};

async function loadAudit(){
  const rows=await api('/api/audit');const b=document.getElementById('auditBody');b.innerHTML='';
  rows.forEach(r=>{const tr=document.createElement('tr');
    tr.innerHTML=`<td class="muted">${r.ts}</td><td>${r.action}</td><td class="muted">${r.detail||''}</td>`;b.appendChild(tr);});
}

refreshOllama();loadJobs();loadProfile();loadSettings();
setInterval(refreshOllama,15000);
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(title="OpenJobPilot")


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/api/ollama")
def api_ollama():
    return ollama_status(load_config())


@app.get("/api/profile")
def api_get_profile():
    return get_profile()


@app.post("/api/profile")
async def api_set_profile(request: Request):
    data = await request.json()
    set_profile(data)
    return {"ok": True}


@app.get("/api/config")
def api_get_config():
    return load_config()


@app.post("/api/config")
async def api_set_config(request: Request):
    cfg = load_config()
    incoming = await request.json()
    cfg.update(incoming)
    save_config(cfg)
    audit("config_updated", "")
    return {"ok": True}


@app.post("/api/collect")
def api_collect():
    return collect_all(load_config())


@app.get("/api/jobs")
def api_jobs(q: str = "", status: str = "", min_score: int = 0):
    sql = "SELECT * FROM jobs WHERE score >= ?"
    params = [min_score]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR description LIKE ?)"
        like = "%" + q + "%"
        params += [like, like, like]
    sql += " ORDER BY score DESC, fetched_at DESC LIMIT 500"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/jobs/{job_id}/status")
async def api_status(job_id: int, request: Request):
    data = await request.json()
    with _db_lock, db() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?",
                    (data.get("status", "new"), job_id))
    audit("status_change", "job_id={} -> {}".format(job_id, data.get("status")))
    return {"ok": True}


@app.post("/api/generate/{job_id}")
def api_generate(job_id: int):
    cfg = load_config()
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    job = dict(row)
    docs = generate_documents(job, get_profile(), cfg)
    return {"job": job, **docs}


@app.post("/api/prefill/{job_id}")
def api_prefill(job_id: int):
    with db() as conn:
        row = conn.execute("SELECT url FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Runs synchronously and opens a visible browser on the machine running this app.
    return prefill_application(row["url"])


@app.get("/api/audit")
def api_audit():
    with db() as conn:
        rows = conn.execute("SELECT ts, action, detail FROM audit ORDER BY id DESC LIMIT 300").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/export")
def api_export(format: str = Query("csv")):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY score DESC").fetchall()]
    if format == "json":
        return JSONResponse(rows, headers={"Content-Disposition": "attachment; filename=jobs.json"})
    buf = StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=jobs.csv"})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    init_db()
    cfg = load_config()
    audit("startup", "OpenJobPilot started")
    url = "http://{}:{}".format(cfg["host"], cfg["port"])
    print("\n  OpenJobPilot is running.")
    print("  Open this in your browser:  " + url)
    print("  Data + audit trail in:      " + DATA_DIR + "\n")
    uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning")


if __name__ == "__main__":
    main()
