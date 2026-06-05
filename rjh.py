"""
RJH - Reverse Job Hunting.

An ethical, open-source, local-first job-application copilot.

SPDX-License-Identifier: GPL-3.0-or-later
Copyright (C) 2026 Ideotion. Licensed under the GNU General Public License v3.0
or later. See the LICENSE file for the full text. This program comes with
ABSOLUTELY NO WARRANTY.

WHAT IT DOES
  1. Collects job postings from config-driven source adapters (RSS/Atom feeds),
     respecting robots.txt and per-domain rate limits.
  2. Stores them in a searchable local SQLite database with duplicate detection
     (URL canonicalization + content hashing).
  3. Scores each posting against your master profile so the best fits surface first.
  4. Uses your LOCAL Ollama instance to tailor a resume and cover letter per job.
     Nothing about you ever leaves your machine.
  5. Optionally pre-fills the application form in a real browser (Playwright) and
     then STOPS. You review, edit, and click submit yourself. That click is the
     only thing that ever sends anything.
  6. Logs every action to the database and to a dated markdown audit trail.

ETHICS (non-negotiable, baked in)
  - Respects robots.txt for every domain before fetching.
  - Rate-limits itself per domain.
  - Prefers official feeds/APIs and public career pages over scraping.
  - Never auto-submits. A human gives the final go on every application.
  - Auto-submitting to sites like LinkedIn/Indeed violates their Terms of Service
    and risks account bans; this tool deliberately does not do that.

INSTALL
  # one-liner (clones the repo, sets up a venv, installs core deps):
  curl -fsSL https://raw.githubusercontent.com/ideotion/RJH/main/install.sh | sh
  # ...or manually:
  python3 -m venv venv && source venv/bin/activate
  pip install fastapi uvicorn requests
  # optional, only for the form pre-fill assistant (Firefox by default):
  pip install playwright && playwright install firefox
  # optional, only for importing PDF/ODT resumes:
  pip install pypdf odfpy
  # local LLM: install/manage Ollama and models from the Settings -> Setup tab
  # in the GUI, or manually from https://ollama.com (then:  ollama pull mistral)

RUN
  python3 rjh.py
  # then open the printed URL (default http://127.0.0.1:8765) in your browser.
"""

import os
import re
import sys
import csv
import json
import time
import html
import shutil
import hashlib
import sqlite3
import platform
import threading
import subprocess
import datetime as dt
from io import StringIO, BytesIO
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib import robotparser
from xml.etree import ElementTree as ET

import requests

try:
    from fastapi import FastAPI, Request, Query
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    import uvicorn
except ImportError:
    raise SystemExit("Missing dependencies. Run:  pip install fastapi uvicorn requests")

# Optional dependencies. Each is guarded so the app runs fully without it, and
# the guards catch BaseException (not just ImportError): a broken optional dep —
# e.g. a package whose native extension panics on import — must disable only its
# own feature, never take down the whole app.
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    PLAYWRIGHT_AVAILABLE = True
except BaseException:
    PLAYWRIGHT_AVAILABLE = False

# Only needed to import PDF / ODT resumes. Both parse fully locally.
try:
    import pypdf  # noqa: F401
    PYPDF_AVAILABLE = True
except BaseException:
    PYPDF_AVAILABLE = False
try:
    import odf  # noqa: F401  (odfpy)
    ODFPY_AVAILABLE = True
except BaseException:
    ODFPY_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Paths and identity
# --------------------------------------------------------------------------- #

DATA_DIR = os.path.join(os.getcwd(), "rjh_data")
DB_PATH = os.path.join(DATA_DIR, "rjh.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
USER_AGENT = ("RJH/0.2 (Reverse Job Hunting; +https://github.com/ideotion/RJH; "
              "ethical open-source job copilot)")


DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "mistral",
    "output_language": "auto",          # auto = match the posting's language
    # AI document tools (Ollama) are an OPTIONAL add-on. The scraper, database,
    # search/sort, profile and pre-fill all work with this off.
    "llm_enabled": True,
    # Browser engine for the optional pre-fill step: firefox, chromium or webkit.
    # Install the chosen one with `playwright install <engine>`.
    "browser": "firefox",
    "rate_limit_seconds": 5,            # minimum seconds between hits to one domain
    "request_timeout": 20,
    # Country preference for Western & Northern Europe. ISO-2 codes, ordered.
    "preferred_countries": ["NL", "DE", "SE", "DK", "NO", "FI", "BE",
                            "LU", "AT", "CH", "IE", "IS", "GB", "FR"],
    # Config-driven sources.
    #   type "demo"     -> bundled offline sample, so the app works on first run.
    #   type "rss"      -> any RSS/Atom feed.
    #   type "json_api" -> any JSON jobs API; "map" overrides field detection,
    #                      "root" points at the list, "default_country" fills gaps.
    # Network sources ship DISABLED so nothing is fetched until you opt in. Flip
    # "enabled" to true to use one. Arbeitnow is a real, free, no-auth European
    # job API included as a ready-to-run example. EURES (the official EU portal)
    # is the recommended primary source: paste an official EURES/national-PES feed
    # URL and enable it.
    "sources": [
        {
            "name": "DEMO (offline sample)",
            "type": "demo",
            "enabled": True,
            "default_country": "EU"
        },
        {
            # Sweden's Public Employment Service (Arbetsförmedlingen) open
            # JobTech API. Free, no key. Nested fields use dotted paths.
            # Empty q returns the most recent ads; tune q/limit in the URL.
            "name": "Arbetsformedlingen / JobTech (Swedish PES)",
            "type": "json_api",
            "enabled": False,
            "url": "https://jobsearch.api.jobtechdev.se/search?q=&limit=50",
            "root": "hits",
            "map": {
                "title": "headline",
                "company": "employer.name",
                "location": "workplace_address.municipality",
                "country": "workplace_address.country",
                "url": "webpage_url",
                "description": "description.text",
                "posted_at": "publication_date",
                "salary": "salary_description"
            },
            "default_country": "SE"
        },
        {
            "name": "Arbeitnow (EU job board API)",
            "type": "json_api",
            "enabled": False,
            "url": "https://www.arbeitnow.com/api/job-board-api",
            "root": "data",
            "map": {
                "title": "title",
                "company": "company_name",
                "location": "location",
                "url": "url",
                "description": "description",
                "posted_at": "created_at"
            },
            "default_country": "EU"
        },
        {
            "name": "EURES (official EU portal)",
            "type": "rss",
            "enabled": False,
            "url": "PASTE_AN_OFFICIAL_EURES_OR_PES_FEED_URL_HERE",
            "default_country": "EU"
        }
    ],
    # Per-site pre-fill rules. The first rule whose lowercased "match" is a
    # substring of the job URL (or its host) wins. "fields" maps a category to an
    # ordered list of CSS selectors to try; "uploads" maps a file kind to
    # selectors. Anything not covered here is handled by the multilingual generic
    # fallback at pre-fill time, so adding a site usually needs no rule at all.
    "site_mappings": [
        {
            "name": "Example / template rule (rename and edit for a real site)",
            "match": "example.org",
            "fields": {
                "full_name": ["input[name='name']", "#fullname"],
                "first_name": ["input[name='first_name']", "#firstName"],
                "last_name": ["input[name='last_name']", "#lastName"],
                "email": ["input[type='email']", "input[name='email']"],
                "phone": ["input[type='tel']", "input[name='phone']"],
                "location": ["input[name='location']", "input[name='city']"],
                "linkedin": ["input[name='linkedin']"],
                "cover_letter_text": ["textarea[name='cover_letter']",
                                      "textarea[name='motivation']"]
            },
            "uploads": {
                "resume": ["input[type='file'][name='resume']",
                           "input[type='file'][name='cv']"],
                "cover_letter": ["input[type='file'][name='cover_letter']"]
            }
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
    # backfill any missing keys from the defaults
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
                salary TEXT,
                salary_min INTEGER,
                salary_max INTEGER,
                keywords TEXT,
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
        # Migrate older databases: add columns introduced after first release.
        have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, decl in (("salary", "TEXT"), ("salary_min", "INTEGER"),
                          ("salary_max", "INTEGER"), ("keywords", "TEXT")):
            if col not in have:
                conn.execute("ALTER TABLE jobs ADD COLUMN {} {}".format(col, decl))
        row = conn.execute("SELECT 1 FROM profile WHERE id = 1").fetchone()
        if not row:
            default_profile = {
                "name": "Your Name",
                "headline": "",
                "location": "",
                "email": "",
                "phone": "",
                "linkedin": "",
                "keywords": [],
                "resume_file": "",
                "cover_letter_file": "",
                "resume": "Paste your master resume here, in plain text."
            }
            conn.execute("INSERT INTO profile (id, data) VALUES (1, ?)",
                         (json.dumps(default_profile),))


def audit(action, detail=""):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO audit (ts, action, detail) VALUES (?,?,?)",
                     (ts, action, detail))
    # mirror to a dated markdown trail: "- TS | ACTION | DETAIL"
    fname = dt.datetime.now().strftime("%Y%m%d") + "_RJH_AUDIT_TRAIL.md"
    path = os.path.join(DATA_DIR, fname)
    line = "- {} | {} | {}\n".format(ts, action, (detail or "").replace("\n", " "))
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


def get_documents(job_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT kind, content FROM documents WHERE job_id = ?",
            (job_id,)).fetchall()
    return {r["kind"]: r["content"] for r in rows}


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
    rp = _robots_cache.get(base, "MISSING")
    if rp == "MISSING":
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            # If robots.txt cannot be read, treat the configured feed as opt-in
            # and allow it (the user explicitly pointed us at this feed).
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

    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag == "item":  # RSS
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
                    "cleanroom operations and ERP. Fluent English required. "
                    "Salary: €120,000–150,000 per year plus bonus."},
    {"title": "Head of Regulatory Affairs, Pediatric Pharma",
     "company": "Helsinki Therapeutics", "location": "Helsinki, FI", "country": "FI",
     "url": "https://example.org/jobs/head-regaffairs-helsinki",
     "description": "Own EMA CTD filings and scientific-opinion procedures for "
                    "rare pediatric diseases. Phase 1/2 clinical coordination. "
                    "Compensation 95k–115k EUR depending on experience."},
    {"title": "Director of Operations, Life Sciences CRO",
     "company": "Copenhagen Bioanalytics", "location": "Copenhagen, DK", "country": "DK",
     "url": "https://example.org/jobs/dir-ops-copenhagen",
     "description": "Run a GLP/ICH M10 bioanalytical lab. Method validation, "
                    "study director supervision, client relations, sample stock. "
                    "Annual salary around 90000 EUR."},
]


# Default source-key candidates for the generic JSON-API adapter. A source can
# override any field via its "map" (our field -> source key, or list of keys).
_JSON_FIELD_CANDIDATES = {
    "title": ["title", "job_title", "position", "name", "vacancy_title"],
    "company": ["company_name", "company", "employer", "organization",
                "organisation", "hiringOrganization"],
    "location": ["location", "city", "place", "workLocation", "region"],
    "country": ["country", "country_code", "countryCode"],
    "url": ["url", "link", "apply_url", "application_url", "redirect_url",
            "applyUrl", "jobUrl"],
    "description": ["description", "summary", "details", "content", "body",
                    "job_description"],
    "posted_at": ["created_at", "posted_at", "date", "published", "pubDate",
                  "datePosted", "publication_date"],
    "salary": ["salary", "salary_range", "compensation", "pay", "baseSalary"],
}


def _dig(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _json_first(item, names):
    """Return the first non-empty value among candidate keys. A name containing
    a dot is treated as a nested path (e.g. "employer.name")."""
    for n in names:
        v = _dig(item, n) if "." in n else item.get(n)
        if v not in (None, ""):
            return v
    return ""


def _normalize_date(v):
    """Best-effort: accept ISO strings as-is, convert epoch seconds/millis."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            ts = float(v)
            if ts > 1e12:          # milliseconds
                ts /= 1000
            return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        except Exception:
            return str(v)
    return str(v)


def parse_json_api(text, src):
    """Generic JSON job-API adapter. Finds the list of postings (via src['root']
    dotted path, a top-level list, or common keys) and maps each item to our
    normalized job dict. Pure parsing — no network — so it is unit-testable."""
    data = json.loads(text)
    root = src.get("root")
    items = None
    if root:
        items = _dig(data, root)
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for k in ("data", "jobs", "results", "items", "hits", "postings"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    if not isinstance(items, list):
        return []
    fieldmap = src.get("map", {})
    strip_html = src.get("strip_html", True)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue

        def val(field):
            spec = fieldmap.get(field)
            names = (spec if isinstance(spec, list) else [spec]) if spec \
                else _JSON_FIELD_CANDIDATES.get(field, [])
            return _json_first(it, names)

        url = val("url")
        if not url:
            continue
        desc = str(val("description") or "")
        if strip_html:
            desc = html.unescape(re.sub(r"<[^>]+>", " ", desc))
            desc = re.sub(r"\s+", " ", desc).strip()
        out.append({
            "source": src["name"],
            "title": html.unescape(str(val("title") or "")),
            "company": html.unescape(str(val("company") or "")),
            "location": str(val("location") or ""),
            "country": str(val("country") or src.get("default_country", "")),
            "url": str(url),
            "description": desc,
            "salary": str(val("salary") or ""),
            "posted_at": _normalize_date(val("posted_at")),
        })
    return out


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

    if stype == "json_api":
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
            out = parse_json_api(resp.text, src)
        except Exception as e:
            audit("fetch_error", "{}: {}".format(url, e))
        return out

    return out


def store_job(conn, j, cfg, profile):
    """Dedup, enrich and insert one normalized job dict on an open connection.
    Returns True if inserted, False if it was a duplicate. Shared by the
    collector and by CSV/JSON import so every path enriches identically."""
    if not j.get("url"):
        return False
    uh = url_hash(j["url"])
    ch = content_hash(j.get("title"), j.get("company"), j.get("description"))
    exists = conn.execute(
        "SELECT 1 FROM jobs WHERE url_hash = ? OR content_hash = ?",
        (uh, ch)).fetchone()
    if exists:
        return False
    score = score_job(j, profile, cfg)
    salary, smin, smax, comps = enrich_job(j, profile)
    conn.execute(
        """INSERT INTO jobs (source,url,canonical_url,url_hash,content_hash,
           title,company,location,country,description,salary,salary_min,
           salary_max,keywords,posted_at,fetched_at,score,status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
        (j.get("source"), j.get("url"), canonicalize_url(j["url"]), uh, ch,
         j.get("title"), j.get("company"), j.get("location"),
         j.get("country"), j.get("description"), salary, smin, smax, comps,
         j.get("posted_at"),
         dt.datetime.now().isoformat(timespec="seconds"), score))
    return True


def collect_all(cfg):
    added, skipped = 0, 0
    profile = get_profile()
    for src in cfg.get("sources", []):
        for j in collect_from_source(src, cfg):
            with _db_lock, db() as conn:
                if store_job(conn, j, cfg, profile):
                    added += 1
                else:
                    skipped += 1
    audit("collect", "added={} skipped(dupes)={}".format(added, skipped))
    return {"added": added, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Matching / scoring (0-100)
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
    # country preference bonus, by rank in preferred_countries
    prefs = [c.upper() for c in cfg.get("preferred_countries", [])]
    country = (job.get("country") or "").upper()
    if country and country in prefs:
        rank = prefs.index(country)
        base = min(100, base + max(0, 8 - rank))
    return max(0, min(100, int(base)))


# --------------------------------------------------------------------------- #
# Enrichment: salary + competencies (pure local text parsing, no LLM)
# --------------------------------------------------------------------------- #

_CUR = r"€|£|\$|EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|kr"
# Either grouped thousands (1+ separator groups) or a plain run of digits.
_AMT = r"\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
# currency on either side of an amount, optional k/m suffix, optional range
_SALARY_RE = re.compile(
    r"(?:(?P<c1>" + _CUR + r")\s*)?"
    r"(?P<n1>" + _AMT + r")\s*(?P<s1>[kKmM])?\s*(?:(?P<c2>" + _CUR + r"))?"
    r"(?:\s*(?:-|–|—|to|tot|bis|à)\s*"
    r"(?:(?P<c3>" + _CUR + r")\s*)?(?P<n2>" + _AMT + r")\s*(?P<s2>[kKmM])?"
    r"\s*(?:(?P<c4>" + _CUR + r"))?)?",
    re.IGNORECASE)
_SALARY_HINT = re.compile(
    r"salary|salaries|compensation|\bpay\b|remuneration|wage|gehalt|salaire|"
    r"salaris|loon|l[öo]n|stipendi|retribuzione|sueldo", re.IGNORECASE)


def _to_number(numstr, suffix):
    s = (numstr or "").replace(" ", "")
    s = re.sub(r"[.,](?=\d{3}\b)", "", s)   # drop thousands separators
    s = s.replace(",", ".")                  # any leftover comma -> decimal
    try:
        val = float(s)
    except ValueError:
        return None
    suf = (suffix or "").lower()
    if suf == "k":
        val *= 1000
    elif suf == "m":
        val *= 1_000_000
    return int(val)


def extract_salary(text):
    """Return (raw_text, min, max). Best-effort: only reports a figure that is
    currency-anchored, or a k-suffixed amount sitting near a salary keyword, to
    avoid mistaking unrelated numbers (team sizes, ISO codes) for pay."""
    if not text:
        return "", None, None
    for m in _SALARY_RE.finditer(text):
        has_cur = any(m.group(g) for g in ("c1", "c2", "c3", "c4"))
        has_k = bool(m.group("s1") or m.group("s2"))
        window = text[max(0, m.start() - 40):m.end() + 40]
        near_hint = bool(_SALARY_HINT.search(window))
        if not (has_cur or (has_k and near_hint)):
            continue
        lo = _to_number(m.group("n1"), m.group("s1"))
        hi = _to_number(m.group("n2"), m.group("s2")) if m.group("n2") else None
        if lo is None:
            continue
        if hi is not None and hi < lo:
            lo, hi = hi, lo
        raw = re.sub(r"\s+", " ", m.group(0)).strip(" -–—")
        return raw, lo, (hi if hi is not None else lo)
    return "", None, None


# A modest, multi-domain competency vocabulary. Multi-word entries are matched as
# substrings; single words are matched as whole tokens. Extend freely.
SKILLS = {
    "project management", "product management", "stakeholder management",
    "supply chain", "quality management", "regulatory affairs", "iso 13485",
    "iso 9001", "gmp", "capa", "erp", "lean", "six sigma", "kaizen", "p&l",
    "budgeting", "forecasting", "procurement", "logistics", "operations",
    "leadership", "coaching", "negotiation", "agile", "scrum", "kanban",
    "data analysis", "data science", "machine learning", "deep learning",
    "python", "java", "javascript", "typescript", "golang", "rust", "c++",
    "sql", "nosql", "postgres", "mysql", "mongodb", "spark", "hadoop",
    "docker", "kubernetes", "terraform", "ansible", "linux", "aws", "azure",
    "gcp", "cloud", "devops", "ci/cd", "microservices", "rest", "graphql",
    "react", "vue", "angular", "node", "django", "fastapi", "spring",
    "marketing", "seo", "sem", "crm", "salesforce", "sap", "tableau",
    "power bi", "excel", "accounting", "finance", "compliance", "gdpr",
    "cybersecurity", "penetration testing", "incident response",
    "communication", "english", "german", "french", "dutch", "swedish",
}


def extract_competencies(job, profile, max_items=12):
    """Competency tags for a posting: the profile keywords it actually mentions,
    plus any known skills present in the text. Pure keyword matching."""
    text = (job.get("title", "") + " " + job.get("description", ""))
    low = text.lower()
    toks = tokens(text)
    found = []
    for k in profile.get("keywords", []):
        kl = (k or "").strip().lower()
        if kl and (kl in toks or (" " in kl and kl in low)):
            found.append(k.strip())
    for s in SKILLS:
        if (s in toks) or (" " in s and s in low):
            found.append(s)
    seen, out = set(), []
    for f in found:
        fl = f.lower()
        if fl not in seen:
            seen.add(fl)
            out.append(f)
    return out[:max_items]


def enrich_job(job, profile):
    """Attach salary + competencies to a job dict prior to insert/rescore. An
    explicit `salary` field (e.g. a CSV column) is parsed first; otherwise the
    figure is mined from the description."""
    raw, lo, hi = "", None, None
    if job.get("salary"):
        raw, lo, hi = extract_salary(str(job["salary"]))
        if not raw:                       # keep the provided text even if unparsable
            raw = str(job["salary"]).strip()
    if not raw:
        raw, lo, hi = extract_salary(job.get("description", ""))
    comps = extract_competencies(job, profile)
    return raw, lo, hi, ", ".join(comps)


# --------------------------------------------------------------------------- #
# Ollama (local LLM) document generation
# --------------------------------------------------------------------------- #

def ollama_binary():
    """Path to the locally installed `ollama` binary, or None."""
    return shutil.which("ollama")


def ollama_version():
    path = ollama_binary()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=8)
        return (out.stdout or out.stderr or "").strip()
    except Exception:
        return ""


def ollama_status(cfg):
    """Combined local-engine status: whether the binary is installed, whether the
    server responds, its version, and the models it has pulled."""
    installed = ollama_binary() is not None
    status = {"installed": installed, "up": False, "models": [],
              "version": ollama_version() if installed else "",
              "platform": platform.system().lower()}
    try:
        r = requests.get(cfg["ollama_url"] + "/api/tags", timeout=4)
        if r.status_code == 200:
            status["up"] = True
            status["models"] = [m.get("name") for m in r.json().get("models", [])]
    except Exception:
        pass
    return status


def ollama_install():
    """Guided install of the Ollama engine on Linux via the official script.
    Triggered only by an explicit, confirmed user action in the GUI. Downloads
    from ollama.com (the engine itself — never any candidate data)."""
    if ollama_binary():
        return {"ok": True, "msg": "Ollama is already installed.",
                "version": ollama_version()}
    if platform.system().lower() != "linux":
        return {"ok": False,
                "msg": "Automatic install supports Linux only. Install Ollama "
                       "manually from https://ollama.com/download for your OS."}
    if not shutil.which("curl") and not shutil.which("sh"):
        return {"ok": False, "msg": "curl/sh not found; cannot run the installer."}
    audit("ollama_install_start", "linux official script")
    try:
        # The official one-line installer. Captured so the GUI can show the result.
        proc = subprocess.run("curl -fsSL https://ollama.com/install.sh | sh",
                              shell=True, capture_output=True, text=True, timeout=600)
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-1500:]
        if ollama_binary():
            audit("ollama_install_done", ollama_version())
            return {"ok": True, "msg": "Ollama installed. Start it with `ollama serve`.",
                    "version": ollama_version(), "log": tail}
        audit("ollama_install_failed", "exit={}".format(proc.returncode))
        return {"ok": False,
                "msg": "Installer finished but `ollama` was not found on PATH. "
                       "See the log; you may need to install manually.",
                "log": tail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Install timed out after 10 minutes."}
    except Exception as e:
        return {"ok": False, "msg": "Install error: {}".format(e)}


def ollama_pull(cfg, model):
    """Pull a model into the local Ollama instance via its local API."""
    model = (model or "").strip()
    if not model:
        return {"ok": False, "msg": "No model name given."}
    if not ollama_binary():
        return {"ok": False, "msg": "Ollama is not installed yet. Install it first."}
    audit("ollama_pull_start", model)
    try:
        r = requests.post(cfg["ollama_url"] + "/api/pull",
                          json={"name": model, "stream": False}, timeout=3600)
        if r.status_code != 200:
            return {"ok": False,
                    "msg": "Pull failed ({}). Is `ollama serve` running?".format(
                        r.status_code)}
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            return {"ok": False, "msg": "Pull failed: {}".format(data["error"])}
        audit("ollama_pull_done", model)
        return {"ok": True, "msg": "Model '{}' is ready.".format(model)}
    except requests.exceptions.ConnectionError:
        return {"ok": False,
                "msg": "Could not reach Ollama. Start it with `ollama serve`."}
    except Exception as e:
        return {"ok": False, "msg": "Pull error: {}".format(e)}


def ollama_chat(cfg, system, user):
    payload = {
        "model": cfg["ollama_model"],
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    r = requests.post(cfg["ollama_url"] + "/api/chat", json=payload, timeout=300)
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
        conn.execute("UPDATE jobs SET status = 'generated' WHERE id = ?", (job["id"],))
    audit("generate", "job_id={} title={}".format(job["id"], job["title"]))
    return {"resume": resume, "cover_letter": cover}


# --------------------------------------------------------------------------- #
# Resume import (PDF / ODT / TXT / Markdown) — all parsing is local
# --------------------------------------------------------------------------- #

def _extract_pdf(data):
    if not PYPDF_AVAILABLE:
        return None, ("PDF import needs pypdf. Run:  pip install pypdf")
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text.strip(), None
    except Exception as e:
        return None, "Could not read PDF: {}".format(e)


def _extract_odt(data):
    if not ODFPY_AVAILABLE:
        return None, ("ODT import needs odfpy. Run:  pip install odfpy")
    try:
        from odf.opendocument import load
        from odf import text as odf_text, teletype
        doc = load(BytesIO(data))
        paras = doc.getElementsByType(odf_text.P)
        text = "\n".join(teletype.extractText(p) for p in paras)
        return text.strip(), None
    except Exception as e:
        return None, "Could not read ODT: {}".format(e)


def extract_resume_text(filename, data):
    """Return (text, error). Supports PDF and ODT (optional deps) plus plain
    text / Markdown. Everything is parsed on this machine; nothing is uploaded."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext in ("txt", "text", "md", "markdown"):
        return data.decode("utf-8", "replace").strip(), None
    if ext == "pdf":
        return _extract_pdf(data)
    if ext == "odt":
        return _extract_odt(data)
    return None, ("Unsupported file type '.{}'. Use PDF, ODT, TXT, or MD.".format(ext)
                  if ext else "File has no extension; use PDF, ODT, TXT, or MD.")


# --------------------------------------------------------------------------- #
# Job-list import (CSV / JSON) — feed RJH without configuring a live source
# --------------------------------------------------------------------------- #

# Header aliases so a wide range of CSV/JSON exports map onto our fields.
_JOB_FIELD_ALIASES = {
    "title": ("title", "job_title", "position", "role", "name", "vacancy"),
    "company": ("company", "company_name", "employer", "organization",
                "organisation"),
    "location": ("location", "city", "place", "where", "region"),
    "country": ("country", "country_code"),
    "url": ("url", "link", "apply_url", "application_url", "href", "job_url"),
    "description": ("description", "summary", "details", "text", "body",
                    "job_description"),
    "posted_at": ("posted_at", "date", "published", "posted", "created_at"),
    "salary": ("salary", "salary_range", "compensation", "pay", "wage"),
}


def _map_job_row(row):
    """Map an arbitrary dict (CSV row or JSON object) onto a normalized job.
    Header keys are normalized so "Job Title", "job-title" and "job_title" all
    match the same alias."""
    lower = {}
    for k, v in row.items():
        if k is None:
            continue
        key = re.sub(r"[\s\-]+", "_", str(k).strip().lower())
        lower[key] = v
    job = {}
    for field, aliases in _JOB_FIELD_ALIASES.items():
        for a in aliases:
            if a in lower and lower[a] not in (None, ""):
                job[field] = str(lower[a]).strip()
                break
    return job


def parse_jobs_file(filename, data):
    """Return (jobs, error). Accepts JSON (a list, or an object with a
    data/jobs/results/items list) and CSV with a header row."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    text = data.decode("utf-8-sig", "replace")
    if ext == "json" or (not ext and text.lstrip()[:1] in "[{"):
        try:
            obj = json.loads(text)
        except Exception as e:
            return None, "Invalid JSON: {}".format(e)
        if isinstance(obj, dict):
            rows = None
            for k in ("data", "jobs", "results", "items"):
                if isinstance(obj.get(k), list):
                    rows = obj[k]
                    break
            if rows is None:
                return None, ("JSON object has no jobs list "
                              "(expected a top-level array or a data/jobs key).")
        elif isinstance(obj, list):
            rows = obj
        else:
            return None, "JSON must be an array of jobs or an object with a jobs list."
        jobs = [_map_job_row(r) for r in rows if isinstance(r, dict)]
        return jobs, None
    if ext in ("csv", "tsv", "") :
        delim = "\t" if ext == "tsv" else ","
        try:
            reader = csv.DictReader(StringIO(text), delimiter=delim)
            jobs = [_map_job_row(r) for r in reader]
        except Exception as e:
            return None, "Could not parse CSV: {}".format(e)
        return jobs, None
    return None, "Unsupported file type '.{}'. Use CSV or JSON.".format(ext)


def import_jobs(jobs, cfg, source_label):
    """Dedup + enrich + insert imported jobs. Returns counts."""
    profile = get_profile()
    added = skipped = 0
    for j in jobs:
        if not j.get("url"):
            skipped += 1
            continue
        j.setdefault("source", source_label)
        with _db_lock, db() as conn:
            if store_job(conn, j, cfg, profile):
                added += 1
            else:
                skipped += 1
    audit("import_jobs", "added={} skipped={} total={}".format(
        added, skipped, len(jobs)))
    return {"ok": True, "added": added, "skipped": skipped, "total": len(jobs)}


# Canonical columns for the import template. `url` is required; the rest are
# optional. Header names are matched flexibly on import (see _JOB_FIELD_ALIASES).
_TEMPLATE_COLUMNS = ["title", "company", "location", "country", "url",
                     "description", "posted_at", "salary"]
_TEMPLATE_ROWS = [
    {"title": "Senior Software Engineer", "company": "Acme BV",
     "location": "Amsterdam", "country": "NL",
     "url": "https://example.org/jobs/123",
     "description": "Build and operate Python services on AWS. Kubernetes a plus.",
     "posted_at": "2026-06-05", "salary": "EUR 70,000-90,000"},
    {"title": "Regulatory Affairs Manager", "company": "Helsinki Therapeutics",
     "location": "Helsinki", "country": "FI",
     "url": "https://example.org/jobs/456",
     "description": "Own EMA filings for pediatric pharma. English required.",
     "posted_at": "2026-06-04", "salary": "95k-115k EUR"},
]


def build_import_template(fmt):
    """Return (content, media_type, filename) for a CSV or JSON import sample."""
    if fmt == "json":
        return (json.dumps(_TEMPLATE_ROWS, indent=2, ensure_ascii=False),
                "application/json", "rjh_import_template.json")
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=_TEMPLATE_COLUMNS)
    writer.writeheader()
    for row in _TEMPLATE_ROWS:
        writer.writerow(row)
    return buf.getvalue(), "text/csv", "rjh_import_template.csv"


# --------------------------------------------------------------------------- #
# Per-site pre-fill engine (two layers; NEVER submits)
# --------------------------------------------------------------------------- #

# Priority-ordered, multilingual (English, French, German, Dutch, Nordic)
# keyword dictionary for the generic fallback. The first category that matches
# wins, so put the most specific categories first.
FIELD_KEYWORDS = [
    ("email", ["e-mail", "email", "courriel", "e-post", "epost", "correo", "mail"]),
    ("phone", ["telephone", "téléphone", "telefoon", "telefon", "phone", "mobile",
               "mobil", "gsm", "handy", "tlf", "tel"]),
    ("linkedin", ["linkedin"]),
    ("first_name", ["first name", "firstname", "first_name", "given name",
                    "givenname", "prénom", "prenom", "vorname", "voornaam",
                    "förnamn", "fornamn", "fornavn", "etunimi"]),
    ("last_name", ["last name", "lastname", "last_name", "surname", "family name",
                   "familyname", "nom de famille", "nachname", "achternaam",
                   "efternamn", "etternavn", "sukunimi", "nom"]),
    ("full_name", ["full name", "fullname", "full_name", "nom complet", "your name",
                   "name", "naam", "namn", "navn", "nimi"]),
    ("location", ["location", "city", "town", "address", "adresse", "ville", "ort",
                  "stadt", "woonplaats", "plaats", "stad", "postcode", "zip",
                  "ciudad", "by"]),
    ("cover_letter_text", ["cover letter", "coverletter", "cover_letter",
                           "lettre de motivation", "motivation", "anschreiben",
                           "motivatiebrief", "motivering", "motivationsbrev",
                           "personligt brev", "message", "comments"]),
]

FILE_COVER_KEYWORDS = ["cover", "lettre", "motivation", "anschreiben",
                       "motivatiebrief", "motivationsbrev", "personligt brev",
                       "motivering"]
FILE_RESUME_KEYWORDS = ["resume", "résumé", "cv", "curriculum", "lebenslauf"]


def classify_field(haystack):
    for category, keywords in FIELD_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return category
    return None


def classify_file(haystack):
    for kw in FILE_COVER_KEYWORDS:
        if kw in haystack:
            return "cover_letter"
    for kw in FILE_RESUME_KEYWORDS:
        if kw in haystack:
            return "resume"
    return "resume"  # sensible default


def resolve_rule(job_url, cfg):
    """First site_mappings entry whose lowercased match is a substring of the
    job URL or its host."""
    host = (urlparse(job_url).netloc or "").lower()
    lurl = (job_url or "").lower()
    for rule in cfg.get("site_mappings", []):
        m = (rule.get("match") or "").lower()
        if m and (m in lurl or m in host):
            return rule
    return None


def profile_value(category, profile, cover_text):
    name = (profile.get("name") or "").strip()
    parts = name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": profile.get("location", ""),
        "linkedin": profile.get("linkedin", ""),
        "cover_letter_text": cover_text or "",
    }.get(category, "")


def _write_tmp(name, content):
    if not content:
        return None
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def resolve_upload_path(kind, profile, resume_text, cover_text):
    """Prefer a curated PDF path from the profile if it exists; otherwise fall
    back to a temp .txt of the generated document."""
    if kind == "resume":
        p = profile.get("resume_file", "")
        if p and os.path.exists(p):
            return p
        return _write_tmp("RJH_resume.txt", resume_text or "")
    p = profile.get("cover_letter_file", "")
    if p and os.path.exists(p):
        return p
    return _write_tmp("RJH_cover_letter.txt", cover_text or "")


def prefill_application(job_id, job_url, cfg):
    """Opens the posting in a VISIBLE browser, pre-fills what it can using the
    explicit site rule first and a multilingual generic fallback second, prints
    a report, then HANDS CONTROL TO THE HUMAN. It never clicks submit."""
    if not PLAYWRIGHT_AVAILABLE:
        return {"ok": False,
                "msg": "Playwright not installed. Run: pip install playwright "
                       "&& playwright install firefox",
                "report": []}

    from playwright.sync_api import sync_playwright

    # Which browser engine to drive: firefox, chromium or webkit. All are
    # supported by Playwright; install the chosen one with `playwright install
    # <engine>`. Defaults to firefox.
    engine = (cfg.get("browser") or "firefox").strip().lower()
    if engine not in ("firefox", "chromium", "webkit"):
        engine = "firefox"

    profile = get_profile()
    docs = get_documents(job_id)
    cover_text = docs.get("cover_letter", "")
    resume_text = docs.get("resume", "")
    rule = resolve_rule(job_url, cfg)

    report = []
    filled_categories = set()

    try:
        with sync_playwright() as pw:
            try:
                browser = getattr(pw, engine).launch(headless=False)
            except Exception as e:
                return {"ok": False,
                        "msg": "Could not launch {0}. Install it with: "
                               "playwright install {0}  ({1})".format(engine, e),
                        "report": []}
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(job_url, wait_until="domcontentloaded")

            # ---- Layer 1: explicit site rule -------------------------------
            if rule:
                report.append("Applied site rule: " + rule.get("name", "(unnamed)"))
                for category, selectors in (rule.get("fields") or {}).items():
                    value = profile_value(category, profile, cover_text)
                    if not value:
                        continue
                    for sel in selectors:
                        try:
                            page.fill(sel, value, timeout=1500)
                            filled_categories.add(category)
                            report.append("[rule] {} -> {}".format(category, sel))
                            break
                        except Exception:
                            continue
                for kind, selectors in (rule.get("uploads") or {}).items():
                    path = resolve_upload_path(kind, profile, resume_text, cover_text)
                    if not path:
                        continue
                    for sel in selectors:
                        try:
                            page.set_input_files(sel, path, timeout=1500)
                            report.append("[rule] upload {} -> {} ({})".format(
                                kind, sel, os.path.basename(path)))
                            break
                        except Exception:
                            continue

            # ---- Layer 2: multilingual generic fallback (text fields) ------
            for el in page.query_selector_all("input, textarea"):
                try:
                    tag = (el.evaluate("e => e.tagName.toLowerCase()") or "")
                    typ = (el.get_attribute("type") or "").lower()
                    if tag == "input" and typ in ("hidden", "submit", "button",
                                                   "checkbox", "radio", "file",
                                                   "image", "reset", "range", "color"):
                        continue
                    try:
                        current = el.input_value()
                    except Exception:
                        current = ""
                    if current and current.strip():
                        continue
                    fid = el.get_attribute("id") or ""
                    label_text = ""
                    if fid:
                        try:
                            lab = page.query_selector("label[for='{}']".format(fid))
                            if lab:
                                label_text = lab.inner_text()
                        except Exception:
                            pass
                    if not label_text:
                        try:
                            label_text = el.evaluate(
                                "e => { const l = e.closest('label');"
                                " return l ? l.innerText : ''; }") or ""
                        except Exception:
                            label_text = ""
                    haystack = " ".join([
                        el.get_attribute("name") or "",
                        fid,
                        el.get_attribute("placeholder") or "",
                        el.get_attribute("aria-label") or "",
                        el.get_attribute("autocomplete") or "",
                        el.get_attribute("title") or "",
                        label_text,
                    ]).lower()
                    category = classify_field(haystack)
                    if not category or category in filled_categories:
                        continue
                    value = profile_value(category, profile, cover_text)
                    if not value:
                        continue
                    el.fill(value)
                    filled_categories.add(category)
                    report.append("[generic] {} <- '{}'".format(
                        category, haystack.strip()[:60]))
                except Exception:
                    continue

            # ---- Layer 2: generic file uploads -----------------------------
            for el in page.query_selector_all("input[type='file']"):
                try:
                    haystack = " ".join([
                        el.get_attribute("name") or "",
                        el.get_attribute("id") or "",
                        el.get_attribute("aria-label") or "",
                        el.get_attribute("title") or "",
                    ]).lower()
                    kind = classify_file(haystack)
                    path = resolve_upload_path(kind, profile, resume_text, cover_text)
                    if not path:
                        continue
                    el.set_input_files(path)
                    report.append("[generic] upload {} ({})".format(
                        kind, os.path.basename(path)))
                except Exception:
                    continue

            print("\n=== RJH pre-fill report ===")
            for line in report:
                print("  " + line)
            if not report:
                print("  (nothing matched automatically — fill it in by hand)")
            print("RJH never submits. Review the form, finish it, and submit "
                  "MANUALLY in the browser.\n")
            audit("prefill", "job_id={} filled={}".format(
                job_id, ",".join(sorted(filled_categories)) or "none"))

            # Park here. The human reviews, finishes, and submits manually.
            input("Form pre-filled. Review and submit MANUALLY in the browser, "
                  "then press Enter here to close...")
            browser.close()
        return {"ok": True,
                "msg": "Pre-fill finished. Submit was left to you.",
                "report": report}
    except Exception as e:
        return {"ok": False, "msg": "Pre-fill error: {}".format(e), "report": report}


# --------------------------------------------------------------------------- #
# Web GUI (served to your real browser; no external assets, works fully offline)
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>RJH — Reverse Job Hunting</title>
<style>
  :root{--bg:#0b0f17;--card:#141b27;--line:#243044;--txt:#e6edf3;--mut:#8b98ad;
        --acc:#6366f1;--acc2:#a855f7;--good:#3fb950;--warn:#d29922;--bad:#f85149;}
  *{box-sizing:border-box;font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial}
  body{margin:0;background:var(--bg);color:var(--txt);font-size:14px}
  header{display:flex;align-items:center;gap:16px;padding:14px 22px;
    border-bottom:1px solid var(--line);position:sticky;top:0;
    background:linear-gradient(180deg,#0b0f17,#0b0f17ee);z-index:5;backdrop-filter:blur(4px)}
  .logo{display:flex;align-items:baseline;gap:10px}
  .logo h1{font-size:20px;margin:0;background:linear-gradient(90deg,var(--acc),var(--acc2));
    -webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800}
  .logo .tag{font-size:11px;color:var(--mut)}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block;background:var(--bad);
    box-shadow:0 0 7px var(--bad)}
  .dot.up{background:var(--good);box-shadow:0 0 7px var(--good)}
  .ollama{font-size:11px;color:var(--mut);border:1px solid var(--line);
    padding:4px 9px;border-radius:20px;display:flex;align-items:center;gap:7px}
  nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap;background:var(--card);
    border:1px solid var(--line);border-radius:10px;padding:4px}
  nav button{background:transparent;color:var(--mut);border:0;padding:7px 13px;
    border-radius:7px;cursor:pointer;font-size:13px}
  nav button.active{color:#fff;background:linear-gradient(90deg,var(--acc),var(--acc2))}
  main{padding:22px;max-width:1150px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:16px;margin-bottom:16px}
  .banner{background:linear-gradient(90deg,#1c2740,#241c40);border:1px solid var(--line);
    border-radius:10px;padding:12px 14px;margin-bottom:16px;font-size:13px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  button.btn{background:linear-gradient(90deg,var(--acc),var(--acc2));color:#fff;border:0;
    padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
  button.btn.sec{background:transparent;color:var(--txt);border:1px solid var(--line)}
  button.btn:disabled{opacity:.5;cursor:not-allowed}
  a.btn{text-decoration:none;display:inline-block}
  th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
  th.sortable:hover{color:#fff}
  th.sortable .arr{opacity:.45;font-size:10px;margin-left:3px}
  th.sortable.active{color:#fff}
  th.sortable.active .arr{opacity:1;color:var(--acc2)}
  .tags{display:flex;flex-wrap:wrap;gap:4px;max-width:230px}
  .tag{font-size:10px;padding:1px 7px;border:1px solid var(--line);border-radius:10px;
    color:var(--muted);white-space:nowrap}
  .sal{font-variant-numeric:tabular-nums;white-space:nowrap}
  input,select,textarea{background:var(--bg);color:var(--txt);border:1px solid var(--line);
    border-radius:8px;padding:8px;font-size:13px}
  textarea{width:100%;min-height:120px;font-family:ui-monospace,Menlo,Consolas,monospace}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px}
  .badge{font-weight:700;padding:3px 9px;border-radius:8px;font-size:12px;color:#fff}
  .badge.g{background:var(--good)}.badge.a{background:var(--warn)}.badge.r{background:var(--bad)}
  .pill{font-size:11px;padding:3px 9px;border-radius:12px;border:1px solid var(--line);
    color:var(--mut);text-transform:capitalize}
  .pill.new{color:#9ecbff;border-color:#1f6feb}
  .pill.shortlisted{color:#d2a8ff;border-color:#8957e5}
  .pill.generated{color:#7ee787;border-color:#2ea043}
  .pill.applied{color:#ffd; border-color:var(--warn)}
  .pill.archived{color:var(--mut)}
  .muted{color:var(--mut)}
  a{color:#9ecbff}
  .hide{display:none}
  label{display:block;margin:9px 0 3px;color:var(--mut);font-size:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .stat .n{font-size:26px;font-weight:800}
  .stat .l{font-size:12px;color:var(--mut)}
  .group-title{font-size:13px;color:var(--acc2);font-weight:700;margin:4px 0 8px}
  .toast{position:fixed;bottom:18px;right:18px;background:var(--card);
    border:1px solid var(--line);padding:11px 15px;border-radius:9px;z-index:20}
  #overlay{position:fixed;inset:0;background:#0008;display:none;align-items:center;
    justify-content:center;z-index:30;backdrop-filter:blur(2px)}
  #overlay.show{display:flex}
  .spinner{width:42px;height:42px;border:4px solid var(--line);border-top-color:var(--acc);
    border-radius:50%;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .ov-box{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:22px 28px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:12px;max-width:420px}
  .ov-box .sub{font-size:12px;color:var(--mut);line-height:1.5}
  button.btn{transition:filter .15s,opacity .15s}
  button.btn:hover:not(:disabled){filter:brightness(1.12)}
  nav button{transition:color .15s,background .15s}
  tbody tr{transition:background .12s}
  tbody tr:hover{background:#1a2336}
  input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc);
    box-shadow:0 0 0 2px rgba(99,102,241,.25)}
  .stat{transition:border-color .15s}
  .stat:hover{border-color:var(--acc)}
  .toast{box-shadow:0 6px 24px #0007;border-left:3px solid var(--acc)}
  .toast.err{border-left-color:var(--bad)}
  .toast.ok{border-left-color:var(--good)}
  .hint{font-size:11px;color:var(--mut);margin-top:6px}
  .docmeta{font-size:12px;color:var(--mut);margin-left:auto;align-self:center}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:#2a3650;border-radius:8px}
  ::-webkit-scrollbar-track{background:transparent}
  @media (max-width:760px){
    .stats{grid-template-columns:1fr 1fr}
    .grid2,.grid3{grid-template-columns:1fr}
    nav{margin-left:0;width:100%}
    header{flex-wrap:wrap}
    main{padding:14px}
  }
</style></head>
<body>
<header>
  <div class="logo"><h1>RJH</h1><span class="tag">Reverse Job Hunting</span></div>
  <span class="ollama" id="ollamaWrap"><span id="ollamaDot" class="dot"></span><span id="ollamaTxt">Ollama: checking…</span></span>
  <nav>
    <button data-tab="jobs" class="active">Jobs</button>
    <button data-tab="docs" id="tabDocsBtn">Documents</button>
    <button data-tab="profile">Profile</button>
    <button data-tab="settings">Settings</button>
    <button data-tab="audit">Audit</button>
  </nav>
</header>
<main>

<section id="tab-jobs">
  <div class="stats">
    <div class="stat"><div class="n" id="stTotal">0</div><div class="l">Total jobs</div></div>
    <div class="stat"><div class="n" id="stShort">0</div><div class="l">Shortlisted</div></div>
    <div class="stat"><div class="n" id="stGen">0</div><div class="l">Generated</div></div>
    <div class="stat"><div class="n" id="stAvg">0</div><div class="l">Average score</div></div>
  </div>
  <div class="card">
    <div class="row">
      <button class="btn" id="collectBtn">Collect jobs</button>
      <input id="q" placeholder="Search title, company, location, salary, competencies…" style="flex:1;min-width:200px"/>
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
      <button class="btn sec" id="rescoreBtn" title="Recompute score, salary and competencies for all stored jobs">Re-score all</button>
      <input type="file" id="importJobsFile" accept=".csv,.json,.tsv" class="hide"/>
      <button class="btn sec" id="importJobsBtn" title="Import job listings from a CSV or JSON file">Import jobs…</button>
      <a class="btn sec" href="/api/import_template?format=csv" title="Download a sample CSV showing the expected columns">CSV template</a>
      <a class="btn sec" href="/api/import_template?format=json" title="Download a sample JSON file">JSON template</a>
      <a class="btn sec" href="/api/export?format=csv">Export CSV</a>
      <a class="btn sec" href="/api/export?format=json">Export JSON</a>
    </div>
    <div class="hint">Click any column header to sort. Search matches title, company,
      location, country, salary, competencies and description.</div>
  </div>
  <div class="card">
    <table><thead><tr>
      <th class="sortable" data-sort="score">Score</th>
      <th class="sortable" data-sort="title">Title</th>
      <th class="sortable" data-sort="company">Company</th>
      <th class="sortable" data-sort="location">Where</th>
      <th class="sortable" data-sort="salary">Salary</th>
      <th class="sortable" data-sort="keywords">Competencies</th>
      <th class="sortable" data-sort="status">Status</th>
      <th>Actions</th>
    </tr></thead><tbody id="jobsBody"></tbody></table>
  </div>
</section>

<section id="tab-docs" class="hide">
  <div class="banner">🔒 RJH pre-fills the application form and then <b>stops</b>. It never
    clicks submit — you review every field and send it yourself.</div>
  <div class="card" id="docsEmpty"><span class="muted">Select a job and click Generate to
    produce a tailored resume and cover letter here.</span></div>
  <div class="card hide" id="docsPanel">
    <div class="row"><h3 id="docsTitle" style="margin:0"></h3>
      <span class="docmeta" id="docsMeta"></span>
      <button class="btn sec" id="regenBtn">Regenerate</button></div>
    <label>Tailored resume</label>
    <textarea id="resumeBox"></textarea>
    <div class="row"><button class="btn sec" onclick="copyBox('resumeBox')">Copy resume</button></div>
    <label>Cover letter</label>
    <textarea id="coverBox"></textarea>
    <div class="row">
      <button class="btn sec" onclick="copyBox('coverBox')">Copy cover letter</button>
      <button class="btn sec" id="saveDocsBtn">Save edits</button>
      <button class="btn" id="prefillBtn" style="margin-left:auto">Pre-fill application (you submit)</button>
    </div>
    <div class="hint">Edits are saved locally and used during pre-fill. Pre-fill opens a real
      browser, fills what it can, and stops — you review and submit yourself.</div>
  </div>
</section>

<section id="tab-profile" class="hide">
  <div class="card">
    <div class="grid2">
      <div><label>Name</label><input id="pName" style="width:100%"/></div>
      <div><label>Headline</label><input id="pHeadline" style="width:100%"/></div>
      <div><label>Location</label><input id="pLocation" style="width:100%"/></div>
      <div><label>Email</label><input id="pEmail" style="width:100%"/></div>
      <div><label>Phone</label><input id="pPhone" style="width:100%"/></div>
      <div><label>LinkedIn</label><input id="pLinkedin" style="width:100%"/></div>
    </div>
    <label>Keywords (comma separated) — these drive scoring</label>
    <input id="pKeywords" style="width:100%"/>
    <div class="grid2">
      <div><label>Resume file (absolute path, optional — attached during pre-fill)</label>
        <input id="pResumeFile" style="width:100%"/></div>
      <div><label>Cover letter file (absolute path, optional)</label>
        <input id="pCoverFile" style="width:100%"/></div>
    </div>
    <div class="row" style="margin-top:8px;align-items:flex-end">
      <div style="flex:1;min-width:220px">
        <label>Master resume (plain text). Everything is generated only from this.</label>
      </div>
      <div>
        <input type="file" id="importFile" accept=".pdf,.odt,.txt,.md" class="hide"/>
        <button class="btn sec" id="importBtn" title="Import from PDF, ODT, TXT or MD — parsed locally">Import resume…</button>
      </div>
    </div>
    <textarea id="pResume" style="min-height:240px"></textarea>
    <div class="hint" id="importHint">PDF/ODT import is parsed entirely on your machine. Imported
      text lands here for you to review — click <b>Save profile</b> to keep it.</div>
    <div class="row" style="margin-top:8px"><button class="btn" id="saveProfile">Save profile</button></div>
  </div>
</section>

<section id="tab-settings" class="hide">
  <div class="card">
    <div class="group-title">AI document tools (optional add-on)</div>
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
      <input type="checkbox" id="cLlmEnabled" style="width:auto"/>
      Enable AI resume / cover-letter generation (Ollama)
    </label>
    <div class="hint">The scraper, database, search &amp; sort, profile, import and
      browser pre-fill all work with this <b>off</b>. Turn it on only if you want
      local LLM document drafting. Saved with <b>Save settings</b>.</div>
  </div>
  <div id="llmCards">
  <div class="card">
    <div class="group-title">Setup — local model engine (Ollama)</div>
    <div id="setupStatus" class="muted" style="margin-bottom:8px">Checking…</div>
    <div class="row">
      <button class="btn" id="installOllamaBtn">Install Ollama</button>
      <button class="btn sec" id="refreshSetupBtn">Refresh status</button>
    </div>
    <div class="hint">Guided install runs Ollama's official Linux installer after you
      confirm. It downloads the engine from ollama.com — never any of your data.
      On other systems, install manually from
      <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com/download</a>.</div>
    <label style="margin-top:12px">Installed models</label>
    <div id="modelList" class="muted">—</div>
    <div class="row" style="margin-top:8px">
      <input id="pullModel" placeholder="model to pull, e.g. mistral" style="flex:1;min-width:160px"/>
      <button class="btn sec" id="pullModelBtn">Pull model</button>
    </div>
    <div class="hint">A multilingual model (e.g. <code>mistral</code>, <code>llama3</code>) is
      recommended — many European postings are not in English.</div>
  </div>
  <div class="card">
    <div class="group-title">Local model</div>
    <div class="grid3">
      <div><label>Ollama URL</label><input id="cOllamaUrl" style="width:100%"/></div>
      <div><label>Ollama model</label><input id="cOllamaModel" style="width:100%"/></div>
      <div><label>Output language (auto = match posting)</label><input id="cLang" style="width:100%"/></div>
    </div>
    <div class="hint">The active model is what document generation uses. Pull it above first.</div>
  </div>
  </div>
  <div class="card">
    <div class="group-title">Crawling and matching</div>
    <div class="grid3">
      <div><label>Rate limit seconds / domain</label><input id="cRate" type="number" style="width:100%"/></div>
      <div><label>Request timeout (s)</label><input id="cTimeout" type="number" style="width:100%"/></div>
      <div><label>Preferred countries (ISO-2, ordered)</label><input id="cCountries" style="width:100%"/></div>
    </div>
    <div class="grid3" style="margin-top:8px">
      <div><label>Pre-fill browser</label>
        <select id="cBrowser" style="width:100%">
          <option value="firefox">Firefox</option>
          <option value="chromium">Chromium</option>
          <option value="webkit">WebKit</option>
        </select></div>
    </div>
    <div class="hint">The pre-fill step drives this browser engine. Install it once with
      <code>playwright install firefox</code> (or chromium / webkit).</div>
  </div>
  <div class="card">
    <div class="group-title">Sources (JSON)</div>
    <p class="muted" style="margin:0 0 8px">type "rss" = any RSS/Atom feed; set enabled true and a real URL.</p>
    <textarea id="cSources" style="min-height:180px"></textarea>
  </div>
  <div class="card">
    <div class="group-title">Pre-fill site rules (JSON)</div>
    <p class="muted" style="margin:0 0 8px">Explicit CSS-selector rules per site, applied before the
      multilingual generic fallback. Adding a site needs no code change.</p>
    <textarea id="cMappings" style="min-height:200px"></textarea>
  </div>
  <div class="card"><div class="row"><button class="btn" id="saveSettings">Save settings</button>
    <span class="muted">Restart the app to change host/port.</span></div></div>
</section>

<section id="tab-audit" class="hide">
  <div class="card"><table><thead><tr><th>Time</th><th>Action</th><th>Detail</th></tr></thead>
    <tbody id="auditBody"></tbody></table></div>
</section>

</main>
<div id="toast" class="toast hide"></div>
<div id="overlay"><div class="ov-box"><div class="spinner"></div>
  <div id="overlayMsg">Working…</div><div class="sub" id="overlaySub"></div></div></div>
<script>
let currentJob = null;
let jobsCache = {};
function toast(m,kind){const t=document.getElementById('toast');t.textContent=m;
  t.className='toast'+(kind?' '+kind:'');
  setTimeout(()=>t.classList.add('hide'),3000);}
function showOverlay(m,sub){document.getElementById('overlayMsg').textContent=m||'Working…';
  document.getElementById('overlaySub').textContent=sub||'';
  document.getElementById('overlay').classList.add('show');}
function hideOverlay(){document.getElementById('overlay').classList.remove('show');}
function copyBox(id){const el=document.getElementById(id);el.select();
  document.execCommand('copy');toast('Copied');}
async function api(path,opts){const r=await fetch(path,opts);
  if(!r.ok)throw new Error(await r.text());
  const ct=r.headers.get('content-type')||'';return ct.includes('json')?r.json():r.text();}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('main > section').forEach(s=>s.classList.add('hide'));
  document.getElementById('tab-'+b.dataset.tab).classList.remove('hide');
  if(b.dataset.tab==='audit')loadAudit();
  if(b.dataset.tab==='settings')loadSetup();
});

async function refreshOllama(){
  if(!llmEnabled)return;
  try{const s=await api('/api/ollama');
    document.getElementById('ollamaDot').className='dot'+(s.up?' up':'');
    document.getElementById('ollamaTxt').textContent = s.up
      ? 'Ollama: up ('+(s.models.join(', ')||'no models')+')' : 'Ollama: offline';
  }catch(e){}
}

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function loadSetup(){
  let s;
  try{s=await api('/api/ollama');}catch(e){return;}
  const st=document.getElementById('setupStatus');
  const installBtn=document.getElementById('installOllamaBtn');
  let line;
  if(!s.installed){line='⚠️ Ollama is <b>not installed</b>.';installBtn.classList.remove('hide');}
  else{
    installBtn.classList.add('hide');
    const v=s.version?(' — '+esc(s.version)):'';
    line=s.up ? ('✅ Ollama installed and running'+v)
              : ('🟡 Ollama installed'+v+' but not running. Start it: <code>ollama serve</code>');
  }
  if(!s.installed && s.platform!=='linux'){
    line+=' <span class="muted">(automatic install is Linux-only on this build)</span>';
    installBtn.classList.add('hide');
  }
  st.innerHTML=line;
  const ml=document.getElementById('modelList');
  if(s.models && s.models.length){
    ml.innerHTML=s.models.map(m=>`<span class="pill" style="margin:2px 4px 2px 0;cursor:pointer"
      data-use="${esc(m)}" title="Use this model for generation">${esc(m)}</span>`).join('');
    ml.querySelectorAll('[data-use]').forEach(b=>b.onclick=()=>useModel(b.dataset.use));
  }else{
    ml.innerHTML='<span class="muted">No models yet'+(s.installed?' — pull one below.':'.')+'</span>';
  }
}

async function useModel(model){
  document.getElementById('cOllamaModel').value=model;
  try{
    const c=await api('/api/config');c.ollama_model=model;
    await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({ollama_model:model})});
    toast('Active model set to '+model,'ok');refreshOllama();
  }catch(e){toast('Could not set model','err');}
}

document.getElementById('refreshSetupBtn').onclick=loadSetup;
document.getElementById('installOllamaBtn').onclick=async()=>{
  if(!confirm('Run Ollama\'s official Linux installer now? It downloads and installs '
    +'the engine from ollama.com. Continue?'))return;
  showOverlay('Installing Ollama…','Running the official installer — this can take a minute.');
  try{const r=await api('/api/ollama/install',{method:'POST'});
    toast(r.msg, r.ok?'ok':'err');}
  catch(e){toast('Install failed','err');}
  finally{hideOverlay();loadSetup();refreshOllama();}
};
document.getElementById('pullModelBtn').onclick=async()=>{
  const model=document.getElementById('pullModel').value.trim();
  if(!model)return toast('Enter a model name first');
  showOverlay('Pulling '+model+'…','Downloading via your local Ollama. Large models take a while.');
  try{const r=await api('/api/ollama/pull',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({model})});
    toast(r.msg, r.ok?'ok':'err');}
  catch(e){toast('Pull failed','err');}
  finally{hideOverlay();loadSetup();refreshOllama();}
};

function scoreBadge(n){const c=n>=70?'g':(n>=40?'a':'r');
  return '<span class="badge '+c+'">'+n+'</span>';}
function statusPill(s){return '<span class="pill '+s+'">'+s+'</span>';}

let llmEnabled=true;
let sortState={col:'score',dir:'desc'};

function fmtSalary(j){
  if(j.salary) return '<span class="sal">'+esc(j.salary)+'</span>';
  if(j.salary_min){const hi=j.salary_max&&j.salary_max!==j.salary_min?('–'+j.salary_max.toLocaleString()):'';
    return '<span class="sal">'+j.salary_min.toLocaleString()+hi+'</span>';}
  return '<span class="muted">—</span>';
}
function compTags(j){
  const list=(j.keywords||'').split(',').map(s=>s.trim()).filter(Boolean);
  if(!list.length) return '<span class="muted">—</span>';
  const show=list.slice(0,4).map(k=>'<span class="tag">'+esc(k)+'</span>').join('');
  const more=list.length>4?('<span class="tag">+'+(list.length-4)+'</span>'):'';
  return '<div class="tags">'+show+more+'</div>';
}
function updateSortHeaders(){
  document.querySelectorAll('th.sortable').forEach(th=>{
    const active=th.dataset.sort===sortState.col;
    th.classList.toggle('active',active);
    let base=th.querySelector('.lbl');
    if(!base){base=document.createElement('span');base.className='lbl';
      base.textContent=th.textContent;th.textContent='';th.appendChild(base);}
    let arr=th.querySelector('.arr');
    if(!arr){arr=document.createElement('span');arr.className='arr';th.appendChild(arr);}
    arr.textContent=active?(sortState.dir==='asc'?'▲':'▼'):'↕';
  });
}
document.querySelectorAll('th.sortable').forEach(th=>th.onclick=()=>{
  const c=th.dataset.sort;
  if(sortState.col===c){sortState.dir=sortState.dir==='asc'?'desc':'asc';}
  else{sortState.col=c;sortState.dir=(c==='title'||c==='company'||c==='location'||c==='status')?'asc':'desc';}
  loadJobs();
});

async function loadStats(){
  try{const s=await api('/api/stats');
    document.getElementById('stTotal').textContent=s.total;
    document.getElementById('stShort').textContent=(s.by_status||{}).shortlisted||0;
    document.getElementById('stGen').textContent=(s.by_status||{}).generated||0;
    document.getElementById('stAvg').textContent=s.avg_score;
  }catch(e){}
}

async function loadJobs(){
  const q=encodeURIComponent(document.getElementById('q').value);
  const st=document.getElementById('statusFilter').value;
  const ms=document.getElementById('minScore').value||0;
  updateSortHeaders();
  const jobs=await api(`/api/jobs?q=${q}&status=${st}&min_score=${ms}`
    +`&sort=${sortState.col}&dir=${sortState.dir}`);
  jobsCache={};
  const body=document.getElementById('jobsBody');body.innerHTML='';
  if(!jobs.length){body.innerHTML='<tr><td colspan="8" class="muted">No jobs match. '
    +'Click <b>Collect jobs</b> to fetch from your enabled sources.</td></tr>';loadStats();return;}
  jobs.forEach(j=>{
    jobsCache[j.id]=j;
    const hasDocs=(j.status==='generated'||j.status==='applied');
    const genLabel=hasDocs?'Regenerate':'Generate';
    const aiBtns = llmEnabled
      ? `<button class="btn" data-gen="${j.id}">${genLabel}</button>`
        +(hasDocs?`<button class="btn sec" data-docs="${j.id}">Open docs</button>`:'')
      : '';
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${scoreBadge(j.score)}</td>
      <td><a href="${j.url}" target="_blank" rel="noopener">${esc(j.title)||'(untitled)'}</a>
        <div class="muted" style="font-size:11px">${esc(j.source)}</div></td>
      <td>${esc(j.company)}</td>
      <td>${esc(j.location||j.country||'')}</td>
      <td>${fmtSalary(j)}</td>
      <td>${compTags(j)}</td>
      <td>${statusPill(j.status)}</td>
      <td class="row">
        ${aiBtns}
        <button class="btn sec" data-short="${j.id}">Shortlist</button>
        <button class="btn sec" data-arch="${j.id}">Archive</button>
      </td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll('[data-gen]').forEach(b=>b.onclick=()=>generate(b.dataset.gen));
  body.querySelectorAll('[data-docs]').forEach(b=>b.onclick=()=>openExisting(b.dataset.docs));
  body.querySelectorAll('[data-short]').forEach(b=>b.onclick=()=>setStatus(b.dataset.short,'shortlisted'));
  body.querySelectorAll('[data-arch]').forEach(b=>b.onclick=()=>setStatus(b.dataset.arch,'archived'));
  loadStats();
}

async function openExisting(id){
  try{
    const r=await api(`/api/documents/${id}`);
    if(!(r.resume||'').trim() && !(r.cover_letter||'').trim()){
      return toast('No documents yet — click Generate first.');
    }
    openDocs(jobsCache[id],r.resume,r.cover_letter);
  }catch(e){toast('Could not load documents.','err');}
}

async function setStatus(id,status){
  await api(`/api/jobs/${id}/status`,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({status})});
  toast('Status updated','ok');loadJobs();}

async function generate(id){
  showOverlay('Generating locally with Ollama…','This runs entirely on your machine.');
  try{
    const r=await api(`/api/generate/${id}`,{method:'POST'});
    openDocs(r.job,r.resume,r.cover_letter);loadJobs();
    toast('Documents generated','ok');
  }catch(e){toast('Generation failed. Is Ollama running and the model pulled?','err');}
  finally{hideOverlay();}
}

function openDocs(job,resume,cover){
  currentJob=job;
  document.querySelector('nav button[data-tab="docs"]').click();
  document.getElementById('docsEmpty').classList.add('hide');
  document.getElementById('docsPanel').classList.remove('hide');
  document.getElementById('docsTitle').textContent=(job.title||'(untitled)')+' — '+(job.company||'');
  document.getElementById('docsMeta').textContent=statusLabel(job.status);
  document.getElementById('resumeBox').value=resume||'';
  document.getElementById('coverBox').value=cover||'';
}
function statusLabel(s){return s?('status: '+s):'';}

async function saveDocs(silent){
  if(!currentJob)return false;
  try{
    await api(`/api/documents/${currentJob.id}`,{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({resume:document.getElementById('resumeBox').value,
        cover_letter:document.getElementById('coverBox').value})});
    if(!silent){toast('Edits saved','ok');loadJobs();}
    return true;
  }catch(e){if(!silent)toast('Save failed','err');return false;}
}

document.getElementById('saveDocsBtn').onclick=()=>saveDocs(false);
document.getElementById('regenBtn').onclick=()=>{if(currentJob)generate(currentJob.id);};
document.getElementById('prefillBtn').onclick=async()=>{
  if(!currentJob)return;
  if(!confirm('This opens a real browser and pre-fills what it can. It will NOT submit. '
    +'You review and submit yourself. Continue?'))return;
  await saveDocs(true);  // use your latest edits during pre-fill
  showOverlay('Browser opening on the desktop…',
    'Review and submit in that browser, then press Enter in the RJH terminal to close it.');
  try{const r=await api(`/api/prefill/${currentJob.id}`,{method:'POST'});
    toast(r.msg, r.ok===false?'err':'ok');}
  catch(e){toast('Pre-fill failed: '+e.message,'err');}
  finally{hideOverlay();}
};

document.getElementById('collectBtn').onclick=async()=>{
  showOverlay('Collecting jobs…','robots.txt-aware and rate-limited per domain.');
  try{const r=await api('/api/collect',{method:'POST'});
    toast(`Added ${r.added}, skipped ${r.skipped} duplicate(s)`,'ok');loadJobs();}
  catch(e){toast('Collect failed','err');}
  finally{hideOverlay();}
};
document.getElementById('searchBtn').onclick=loadJobs;
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')loadJobs();});
document.getElementById('statusFilter').onchange=loadJobs;
document.getElementById('minScore').onchange=loadJobs;
document.getElementById('rescoreBtn').onclick=async()=>{
  showOverlay('Re-scoring all jobs…','Recomputing score, salary and competencies locally.');
  try{const r=await api('/api/rescore',{method:'POST'});
    toast('Re-scored '+r.rescored+' job(s)','ok');loadJobs();}
  catch(e){toast('Re-score failed','err');}
  finally{hideOverlay();}
};
document.getElementById('importJobsBtn').onclick=()=>document.getElementById('importJobsFile').click();
document.getElementById('importJobsFile').onchange=async(ev)=>{
  const f=ev.target.files[0];if(!f)return;
  showOverlay('Importing jobs from '+f.name+'…','Parsing locally.');
  try{
    const buf=await f.arrayBuffer();
    const r=await fetch('/api/import_jobs?filename='+encodeURIComponent(f.name)
      +'&source='+encodeURIComponent('Import: '+f.name),{method:'POST',body:buf});
    const j=await r.json();
    if(!r.ok||!j.ok){toast(j.error||'Import failed','err');return;}
    toast('Imported '+j.added+', skipped '+j.skipped+' of '+j.total,'ok');loadJobs();
  }catch(e){toast('Import failed: '+e.message,'err');}
  finally{hideOverlay();ev.target.value='';}
};

async function loadProfile(){
  const p=await api('/api/profile');
  pName.value=p.name||'';pHeadline.value=p.headline||'';pEmail.value=p.email||'';
  pPhone.value=p.phone||'';pLocation.value=p.location||'';pLinkedin.value=p.linkedin||'';
  pKeywords.value=(p.keywords||[]).join(', ');
  pResumeFile.value=p.resume_file||'';pCoverFile.value=p.cover_letter_file||'';
  pResume.value=p.resume||'';
}
document.getElementById('saveProfile').onclick=async()=>{
  const body={name:pName.value,headline:pHeadline.value,email:pEmail.value,
    phone:pPhone.value,location:pLocation.value,linkedin:pLinkedin.value,
    keywords:pKeywords.value.split(',').map(s=>s.trim()).filter(Boolean),
    resume_file:pResumeFile.value,cover_letter_file:pCoverFile.value,
    resume:pResume.value};
  await api('/api/profile',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body)});
  toast('Profile saved. New jobs will be scored against it.','ok');
};

document.getElementById('importBtn').onclick=()=>document.getElementById('importFile').click();
document.getElementById('importFile').onchange=async(ev)=>{
  const f=ev.target.files[0];if(!f)return;
  showOverlay('Importing '+f.name+'…','Parsing on your machine — nothing is uploaded.');
  try{
    const buf=await f.arrayBuffer();
    const r=await fetch('/api/import_resume?filename='+encodeURIComponent(f.name),
      {method:'POST',body:buf});
    const j=await r.json();
    if(!r.ok||!j.ok){toast(j.error||'Import failed','err');return;}
    if(!(j.text||'').trim()){toast('No text found — is this a scanned/image PDF?','err');return;}
    document.getElementById('pResume').value=j.text;
    toast('Imported '+j.chars+' characters. Review, then Save profile.','ok');
  }catch(e){toast('Import failed: '+e.message,'err');}
  finally{hideOverlay();ev.target.value='';}
};

function applyLlmUI(enabled){
  llmEnabled=!!enabled;
  document.getElementById('tabDocsBtn').classList.toggle('hide',!llmEnabled);
  document.getElementById('llmCards').classList.toggle('hide',!llmEnabled);
  document.getElementById('ollamaWrap').classList.toggle('hide',!llmEnabled);
  if(!llmEnabled){
    const docsTab=document.getElementById('tab-docs');
    if(!docsTab.classList.contains('hide')){
      document.querySelector('nav button[data-tab="jobs"]').click();
    }
  }
}

async function loadSettings(){
  const c=await api('/api/config');
  cOllamaUrl.value=c.ollama_url;cOllamaModel.value=c.ollama_model;
  cLang.value=c.output_language;cRate.value=c.rate_limit_seconds;
  cTimeout.value=c.request_timeout;
  cCountries.value=(c.preferred_countries||[]).join(', ');
  document.getElementById('cBrowser').value=c.browser||'firefox';
  cSources.value=JSON.stringify(c.sources,null,2);
  cMappings.value=JSON.stringify(c.site_mappings,null,2);
  document.getElementById('cLlmEnabled').checked=c.llm_enabled!==false;
  applyLlmUI(c.llm_enabled!==false);
}
document.getElementById('cLlmEnabled').onchange=(e)=>applyLlmUI(e.target.checked);
document.getElementById('saveSettings').onclick=async()=>{
  let sources,mappings;
  try{sources=JSON.parse(cSources.value);}catch(e){return toast('Sources JSON is invalid','err');}
  try{mappings=JSON.parse(cMappings.value);}catch(e){return toast('Site rules JSON is invalid','err');}
  const body={ollama_url:cOllamaUrl.value,ollama_model:cOllamaModel.value,
    output_language:cLang.value,rate_limit_seconds:Number(cRate.value),
    request_timeout:Number(cTimeout.value),
    llm_enabled:document.getElementById('cLlmEnabled').checked,
    browser:document.getElementById('cBrowser').value,
    preferred_countries:cCountries.value.split(',').map(s=>s.trim()).filter(Boolean),
    sources,site_mappings:mappings};
  await api('/api/config',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body)});
  toast('Settings saved','ok');applyLlmUI(body.llm_enabled);refreshOllama();loadJobs();
};

async function loadAudit(){
  const rows=await api('/api/audit');const b=document.getElementById('auditBody');b.innerHTML='';
  rows.forEach(r=>{const tr=document.createElement('tr');
    tr.innerHTML=`<td class="muted">${r.ts}</td><td>${r.action}</td>
      <td class="muted">${r.detail||''}</td>`;b.appendChild(tr);});
}

(async()=>{
  await loadSettings();      // sets llmEnabled before the first job render
  loadJobs();loadProfile();loadSetup();refreshOllama();
  setInterval(refreshOllama,15000);
})();
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(title="RJH — Reverse Job Hunting")


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/api/ollama")
def api_ollama():
    return ollama_status(load_config())


@app.post("/api/ollama/install")
def api_ollama_install():
    # Explicit, user-confirmed action in the GUI. Linux only.
    return ollama_install()


@app.post("/api/ollama/pull")
async def api_ollama_pull(request: Request):
    data = await request.json()
    return ollama_pull(load_config(), data.get("model", ""))


@app.post("/api/import_resume")
async def api_import_resume(request: Request, filename: str = Query("resume.txt")):
    data = await request.body()
    if not data:
        return JSONResponse({"ok": False, "error": "Empty file."}, status_code=400)
    text, err = extract_resume_text(filename, data)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    audit("import_resume", "{} ({} chars)".format(filename, len(text or "")))
    return {"ok": True, "text": text, "chars": len(text or ""), "filename": filename}


@app.post("/api/import_jobs")
async def api_import_jobs(request: Request,
                         filename: str = Query("jobs.csv"),
                         source: str = Query("File import")):
    data = await request.body()
    if not data:
        return JSONResponse({"ok": False, "error": "Empty file."}, status_code=400)
    jobs, err = parse_jobs_file(filename, data)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if not jobs:
        return JSONResponse({"ok": False, "error": "No rows found in the file."},
                            status_code=400)
    result = import_jobs(jobs, load_config(), source)
    return result


@app.get("/api/import_template")
def api_import_template(format: str = "csv"):
    content, media, filename = build_import_template(
        "json" if format == "json" else "csv")
    return PlainTextResponse(
        content, media_type=media,
        headers={"Content-Disposition": "attachment; filename=" + filename})


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


@app.get("/api/stats")
def api_stats():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status").fetchall()}
        avg = conn.execute("SELECT AVG(score) AS a FROM jobs").fetchone()["a"]
    return {"total": total, "by_status": by_status,
            "avg_score": round(avg, 1) if avg is not None else 0}


# Whitelisted sort columns -> SQL expression. Keeps user input away from SQL.
_SORT_COLUMNS = {
    "score": "score",
    "title": "title COLLATE NOCASE",
    "company": "company COLLATE NOCASE",
    "location": "location COLLATE NOCASE",
    "country": "country COLLATE NOCASE",
    "salary": "salary_min",
    # "more competencies first": count items in the comma-joined keywords field
    "keywords": "(CASE WHEN keywords IS NULL OR keywords = '' THEN 0 "
                "ELSE LENGTH(keywords) - LENGTH(REPLACE(keywords, ',', '')) + 1 END)",
    "posted": "posted_at",
    "fetched": "fetched_at",
    "status": "status",
}


@app.get("/api/jobs")
def api_jobs(q: str = "", status: str = "", min_score: int = 0,
            sort: str = "score", dir: str = "desc"):
    sql = "SELECT * FROM jobs WHERE score >= ?"
    params = [min_score]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if q:
        # free-text search across every human-meaningful field
        fields = ("title", "company", "location", "country", "description",
                  "salary", "keywords", "source", "url")
        sql += " AND (" + " OR ".join(f + " LIKE ?" for f in fields) + ")"
        params += ["%" + q + "%"] * len(fields)
    col = _SORT_COLUMNS.get(sort, "score")
    direction = "ASC" if str(dir).lower() == "asc" else "DESC"
    order = col + " " + direction
    if sort == "salary":
        # always push unknown salaries to the bottom, regardless of direction
        order = "salary_min IS NULL, " + order
    sql += " ORDER BY {}, score DESC, fetched_at DESC LIMIT 500".format(order)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/rescore")
def api_rescore():
    """Recompute score, salary, and competencies for every stored job against
    the current profile/config. Pure local computation; touches no network."""
    cfg = load_config()
    profile = get_profile()
    n = 0
    with _db_lock, db() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        for r in rows:
            job = dict(r)
            score = score_job(job, profile, cfg)
            salary, smin, smax, comps = enrich_job(job, profile)
            conn.execute(
                "UPDATE jobs SET score=?, salary=?, salary_min=?, salary_max=?, "
                "keywords=? WHERE id=?",
                (score, salary, smin, smax, comps, job["id"]))
            n += 1
    audit("rescore", "jobs={}".format(n))
    return {"ok": True, "rescored": n}


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
    if not cfg.get("llm_enabled", True):
        return JSONResponse(
            {"error": "AI document tools are disabled. Enable them in "
                      "Settings to generate documents."}, status_code=409)
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    job = dict(row)
    docs = generate_documents(job, get_profile(), cfg)
    return {"job": job, **docs}


@app.get("/api/documents/{job_id}")
def api_get_documents(job_id: int):
    docs = get_documents(job_id)
    return {"resume": docs.get("resume", ""),
            "cover_letter": docs.get("cover_letter", "")}


@app.post("/api/documents/{job_id}")
async def api_save_documents(job_id: int, request: Request):
    data = await request.json()
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _db_lock, db() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
            return JSONResponse({"error": "not found"}, status_code=404)
        conn.execute("DELETE FROM documents WHERE job_id = ?", (job_id,))
        conn.execute("INSERT INTO documents (job_id,kind,content,created_at) VALUES (?,?,?,?)",
                     (job_id, "resume", data.get("resume", ""), now))
        conn.execute("INSERT INTO documents (job_id,kind,content,created_at) VALUES (?,?,?,?)",
                     (job_id, "cover_letter", data.get("cover_letter", ""), now))
        conn.execute("UPDATE jobs SET status = 'generated' WHERE id = ? "
                     "AND status IN ('new','shortlisted')", (job_id,))
    audit("documents_edited", "job_id={}".format(job_id))
    return {"ok": True}


@app.post("/api/prefill/{job_id}")
def api_prefill(job_id: int):
    cfg = load_config()
    with db() as conn:
        row = conn.execute("SELECT id, url FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Runs synchronously and opens a visible browser on the machine running this app.
    return prefill_application(row["id"], row["url"], cfg)


@app.get("/api/audit")
def api_audit():
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, action, detail FROM audit ORDER BY id DESC LIMIT 300").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/export")
def api_export(format: str = Query("csv")):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs ORDER BY score DESC").fetchall()]
    if format == "json":
        return JSONResponse(rows, headers={
            "Content-Disposition": "attachment; filename=jobs.json"})
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

def _open_browser_when_ready(url, host, port):
    """Wait for the server to accept connections, then open the default browser
    once. Safe to fail (headless machines simply won't have a browser)."""
    import socket
    import webbrowser
    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    for _ in range(150):                      # up to ~15s
        try:
            with socket.create_connection((target, port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    init_db()
    cfg = load_config()
    audit("startup", "RJH started")
    host = cfg["host"]
    port = cfg["port"]
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    url = "http://{}:{}".format(browse_host, port)

    # Auto-open the browser unless explicitly disabled.
    no_browser = ("--no-browser" in sys.argv or
                  os.environ.get("RJH_NO_BROWSER", "").lower() in ("1", "true", "yes"))

    print("\n  RJH — Reverse Job Hunting is running.")
    if no_browser:
        print("  Open this in your browser:  " + url)
    else:
        print("  Opening your browser at:    " + url)
        print("  (disable with --no-browser or RJH_NO_BROWSER=1)")
    print("  Data + audit trail in:      " + DATA_DIR)
    print("  Press Ctrl+C to stop.\n")

    if not no_browser:
        threading.Thread(target=_open_browser_when_ready,
                         args=(url, host, port), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
