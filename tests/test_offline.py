"""Offline unit tests for RJH — pure logic only, no network.

These cover the source-directory / feed-discovery engine (RSS, JSON/ATS,
Personio, Workday), the parsers, the DB round-trip and source promotion, the
rolling-sweep selection, and the diagnostics bundle. Everything runs against a
throwaway SQLite database in a temp directory, so the suite is safe and fast in
CI and needs no third-party packages.

Run:  python -m unittest discover -s tests -p 'test_*.py'
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile

# Import rjh.py from the repo root regardless of where the tests run from.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("rjh", os.path.join(_ROOT, "rjh.py"))
rjh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rjh)


def _use_temp_db():
    """Point rjh at a fresh temp data dir + DB and initialise the schema."""
    d = tempfile.mkdtemp(prefix="rjh-test-")
    rjh.DATA_DIR = d
    rjh.DB_PATH = os.path.join(d, "test.db")
    rjh.CONFIG_PATH = os.path.join(d, "config.json")
    rjh.init_db()
    return d


class CatalogTests(unittest.TestCase):
    def test_catalog_loads(self):
        rjh._catalog_cache = None
        rows = rjh.load_source_catalog()
        self.assertGreater(len(rows), 0)
        for key in ("name", "url", "category", "country", "rss_status"):
            self.assertIn(key, rows[0])


class AtsDetectionTests(unittest.TestCase):
    def test_json_providers(self):
        cases = {
            "https://boards.greenhouse.io/acme": ("greenhouse", "json_api"),
            "https://boards.greenhouse.io/embed/job_board?for=acme&t=1":
                ("greenhouse", "json_api"),
            "https://jobs.lever.co/acme": ("lever", "json_api"),
            "https://jobs.ashbyhq.com/acme": ("ashby", "json_api"),
            "https://acme.recruitee.com/o/x": ("recruitee", "json_api"),
            "https://careers.smartrecruiters.com/acme": ("smartrecruiters", "json_api"),
        }
        for url, (name, typ) in cases.items():
            ep = rjh._ats_endpoint(url)
            self.assertIsNotNone(ep, url)
            self.assertEqual(ep["name"], name)
            self.assertEqual(ep["type"], typ)

    def test_personio_and_workday(self):
        p = rjh._ats_endpoint("https://acme.jobs.personio.de/positions")
        self.assertEqual(p["type"], "personio")
        self.assertEqual(p["url"], "https://acme.jobs.personio.de/xml")
        self.assertEqual(p["base"], "https://acme.jobs.personio.de")

        w = rjh._ats_endpoint("https://nvidia.wd5.myworkdayjobs.com/en-US/CareerSite")
        self.assertEqual(w["type"], "workday")
        self.assertEqual(
            w["url"],
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/CareerSite/jobs")
        self.assertIn("/en-US/CareerSite", w["base"])

    def test_rejections(self):
        # www.recruitee.com is the marketing host, not a board.
        self.assertIsNone(rjh._ats_endpoint("https://www.recruitee.com"))
        # An already-built Workday CXS endpoint must not re-derive.
        self.assertIsNone(rjh._ats_endpoint(
            "https://x.wd3.myworkdayjobs.com/wday/cxs/x/Site/jobs"))
        self.assertIsNone(rjh._ats_endpoint("https://example.com/careers"))


class ParserTests(unittest.TestCase):
    def test_json_api_maps(self):
        payloads = {
            "https://boards.greenhouse.io/acme": {
                "jobs": [{"title": "Eng", "absolute_url": "https://gh/1",
                          "location": {"name": "Berlin"}, "content": "d",
                          "updated_at": "2026-01-01"}]},
            "https://jobs.lever.co/acme": [
                {"text": "Eng", "hostedUrl": "https://lever/1",
                 "categories": {"location": "Paris"}, "descriptionPlain": "d",
                 "createdAt": 1700000000000}],
            "https://jobs.ashbyhq.com/acme": {
                "jobs": [{"title": "Eng", "jobUrl": "https://ashby/1",
                          "location": "Remote", "descriptionPlain": "d"}]},
            "https://acme.recruitee.com": {
                "offers": [{"title": "Eng", "careers_url": "https://r/1",
                            "location": "NL", "description": "d"}]},
            "https://careers.smartrecruiters.com/acme": {
                "content": [{"name": "Eng", "ref": "https://sr/api/1",
                             "location": {"city": "Madrid"}}]},
        }
        for url, payload in payloads.items():
            ep = rjh._ats_endpoint(url)
            src = dict(ep)
            src["name"] = "probe"
            jobs = rjh.parse_json_api(json.dumps(payload), src)
            self.assertEqual(len(jobs), 1, url)
            self.assertTrue(jobs[0]["url"], url)
            self.assertEqual(jobs[0]["title"], "Eng", url)

    def test_personio_xml(self):
        xml = ("<workzag-jobs><position><id>123</id><subcompany>Acme</subcompany>"
               "<office>Berlin</office><name>Senior Eng</name>"
               "<createdAt>2026-01-02</createdAt><jobDescriptions>"
               "<jobDescription><name>Role</name><value>&lt;p&gt;Build&lt;/p&gt;</value>"
               "</jobDescription></jobDescriptions></position></workzag-jobs>")
        jobs = rjh.parse_personio_xml(xml, {"name": "p",
                                            "base": "https://acme.jobs.personio.de"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["url"], "https://acme.jobs.personio.de/job/123")
        self.assertEqual(jobs[0]["location"], "Berlin")
        self.assertEqual(jobs[0]["description"], "Build")
        # Non-feed input is rejected, not crashed.
        self.assertEqual(rjh.parse_personio_xml("<html>x</html>", {"name": "p"}), [])

    def test_workday_json(self):
        payload = {"total": 2, "jobPostings": [
            {"title": "Staff", "externalPath": "/job/B/Staff_R-1",
             "locationsText": "Berlin"},
            {"title": "NoPath"}]}
        base = "https://x.wd5.myworkdayjobs.com/en-US/Site"
        jobs = rjh.parse_workday_json(json.dumps(payload), {"name": "w", "base": base})
        self.assertEqual(len(jobs), 1)            # the path-less posting is skipped
        self.assertEqual(jobs[0]["url"], base + "/job/B/Staff_R-1")
        self.assertEqual(rjh.parse_workday_json("[]", {"name": "w"}), [])

    def test_feed_vs_html(self):
        rss = ("<rss><channel><item><title>Job</title>"
               "<link>https://x/1</link></item></channel></rss>")
        self.assertEqual(len([e for e in rjh.parse_feed(rss) if e.get("url")]), 1)
        self.assertEqual(rjh.parse_feed("<html><body>nope</body></html>"), [])


class DiscoveryHelperTests(unittest.TestCase):
    def test_seed_status(self):
        self.assertIsNone(rjh._seed_status({"rss_status": "unknown - probe required"}))
        self.assertEqual(
            rjh._seed_status({"rss_status": "none - RSS discontinued"})[0],
            "discontinued")
        self.assertEqual(
            rjh._seed_status({"rss_status": "verified - all jobs"})[0], "verified")

    def test_site_root(self):
        self.assertEqual(rjh._site_root("https://www.x.com/jobs/a"), "https://www.x.com")

    def test_feed_link_parser(self):
        p = rjh._FeedLinkParser()
        p.feed('<link rel="alternate" type="application/rss+xml" href="/f.rss">'
               '<script src="https://boards.greenhouse.io/embed/job_board?for=acme">'
               '</script>')
        self.assertIn("/f.rss", p.feeds)
        self.assertEqual((rjh._detect_ats(p.hrefs) or {}).get("name"), "greenhouse")


class DbRoundTripTests(unittest.TestCase):
    def setUp(self):
        _use_temp_db()

    def _save(self, key, **kw):
        rec = {"url": key, "name": "Acme", "feed_url": "", "feed_type": "",
               "method": "probe", "status": "none", "detail": "", "item_count": 0,
               "meta": "", "checked_at": rjh.dt.datetime.now().isoformat()}
        rec.update(kw)
        rjh._save_catalog_feed(rec)

    def test_catalog_with_status_reflects_cache(self):
        cat = rjh.load_source_catalog()
        key = rjh.canonicalize_url(cat[0]["url"])
        self._save(key, status="verified", feed_url="https://feed", feed_type="rss",
                   item_count=3)
        rows = {r["key"]: r for r in rjh.catalog_with_status()}
        self.assertEqual(rows[key]["status"], "verified")
        self.assertEqual(rows[key]["item_count"], 3)

    def test_promote_json_source(self):
        ep = rjh._ats_endpoint("https://jobs.lever.co/acme")
        key = rjh.canonicalize_url("https://jobs.lever.co/acme")
        self._save(key, status="verified", feed_url=ep["url"], feed_type="json_api",
                   meta=json.dumps({k: ep[k] for k in ("root", "map", "base")
                                    if k in ep}))
        cfg = {"sources": []}
        ok, _ = rjh.add_catalog_source(key, cfg)
        self.assertTrue(ok)
        added = cfg["sources"][-1]
        self.assertEqual(added["type"], "json_api")
        self.assertEqual(added["url"], ep["url"])
        self.assertEqual(added["map"], ep["map"])
        # Dedup on a second promote.
        ok2, _ = rjh.add_catalog_source(key, cfg)
        self.assertFalse(ok2)

    def test_promote_workday_carries_base(self):
        ep = rjh._ats_endpoint("https://x.wd5.myworkdayjobs.com/Site")
        key = rjh.canonicalize_url("https://x.wd5.myworkdayjobs.com/Site")
        self._save(key, status="verified", feed_url=ep["url"], feed_type="workday",
                   meta=json.dumps({"base": ep["base"]}))
        cfg = {"sources": []}
        ok, _ = rjh.add_catalog_source(key, cfg)
        self.assertTrue(ok)
        self.assertEqual(cfg["sources"][-1]["type"], "workday")
        self.assertEqual(cfg["sources"][-1]["base"], ep["base"])

    def test_promote_requires_verified(self):
        key = rjh.canonicalize_url("https://eures.europa.eu")
        self._save(key, status="none")
        ok, msg = rjh.add_catalog_source(key, {"sources": []})
        self.assertFalse(ok)


class SweepSelectionTests(unittest.TestCase):
    def setUp(self):
        _use_temp_db()

    def test_cap_due_and_force(self):
        seen = []

        def stub(entry, cfg):
            key = rjh.canonicalize_url(entry["url"])
            seen.append(key)
            rec = {"url": key, "name": entry["name"], "feed_url": "",
                   "feed_type": "", "method": "probe", "status": "none",
                   "detail": "", "item_count": 0, "meta": "",
                   "checked_at": rjh.dt.datetime.now().isoformat()}
            rjh._save_catalog_feed(rec)
            return rec

        orig = rjh.discover_feed_for_source
        rjh.discover_feed_for_source = stub
        try:
            cfg = dict(rjh.DEFAULT_CONFIG)
            rjh.discover_sweep(cfg, limit=5)
            self.assertEqual(len(seen), 5)            # cap respected
            seen.clear()
            rjh.discover_sweep(cfg, limit=5)          # next batch, no overlap
            self.assertEqual(len(seen), 5)
            seen.clear()
            rjh.discover_sweep(cfg, limit=3, force=True)   # re-probe regardless
            self.assertEqual(len(seen), 3)
        finally:
            rjh.discover_feed_for_source = orig


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        _use_temp_db()

    def test_redaction(self):
        red = rjh._redact({"password": "hunter2", "username": "jane@x.com",
                           "nested": [{"api_key": "abc"}], "ollama_url": "http://h"})
        self.assertEqual(red["password"], "***redacted***")
        self.assertTrue(red["username"].endswith("@x.com"))
        self.assertNotIn("jane", red["username"])
        self.assertEqual(red["nested"][0]["api_key"], "***redacted***")
        self.assertEqual(red["ollama_url"], "http://h")

    def test_collect_diagnostics(self):
        cfg = {"sources": [{"type": "rss", "enabled": True, "url": "u"}],
               "llm_enabled": False,
               "email_ingest": {"password": "secret", "username": "a@b.com"}}
        diag = rjh.collect_diagnostics(cfg)
        for key in ("generated_at", "environment", "counts", "config",
                    "sources_summary", "discovery_by_status", "recent_audit"):
            self.assertIn(key, diag)
        self.assertEqual(diag["config"]["email_ingest"]["password"], "***redacted***")
        self.assertEqual(diag["sources_summary"]["configured"], 1)

    def test_diagnostics_zip(self):
        data = rjh.diagnostics_zip({"sources": [], "llm_enabled": False})
        self.assertTrue(data.startswith(b"PK"))
        with zipfile.ZipFile(rjh.BytesIO(data)) as z:
            names = set(z.namelist())
        for expected in ("diagnostics.json", "config.redacted.json",
                         "audit.recent.md", "discovery.json", "scheduler.json"):
            self.assertIn(expected, names)


class DiscoverAllTests(unittest.TestCase):
    """The "Find all feeds & collect" path: probe everything, then promote and
    collect from the ones that verify. Network calls are stubbed."""

    def setUp(self):
        _use_temp_db()
        self._disc = rjh.discover_feed_for_source
        self._coll = rjh.collect_from_source

    def tearDown(self):
        rjh.discover_feed_for_source = self._disc
        rjh.collect_from_source = self._coll

    def test_discover_all_promotes_and_collects(self):
        state = {"n": 0}

        def fake_disc(entry, cfg):
            state["n"] += 1
            verified = state["n"] <= 3
            key = rjh.canonicalize_url(entry["url"])
            rec = {"url": key, "name": entry["name"],
                   "feed_url": ("https://feed/%d" % state["n"]) if verified else "",
                   "feed_type": "rss" if verified else "", "method": "x",
                   "status": "verified" if verified else "none", "detail": "",
                   "item_count": 2 if verified else 0, "meta": "",
                   "checked_at": rjh.dt.datetime.now().isoformat()}
            rjh._save_catalog_feed(rec)
            return rec

        def fake_collect(src, cfg):
            return [{"source": src["name"], "title": "Role %s-%d" % (src["url"][-1], i),
                     "company": "C%d" % i, "country": "DE",
                     "url": src["url"] + "/job%d" % i,
                     "description": "python role %d" % i, "posted_at": ""}
                    for i in (1, 2)]

        rjh.discover_feed_for_source = fake_disc
        rjh.collect_from_source = fake_collect

        cfg = dict(rjh.DEFAULT_CONFIG)
        cfg["sources"] = []
        res = rjh.discover_all(cfg, force=True)
        self.assertEqual(res["verified"], 3)
        self.assertEqual(res["added_jobs"], 6)            # 2 per verified source
        promoted = [s for s in cfg["sources"] if "(discovered)" in s.get("name", "")]
        self.assertEqual(len(promoted), 3)

        prog = rjh.discovery_progress()
        self.assertFalse(prog["running"])
        self.assertEqual(prog["done"], prog["total"])

        # Idempotent: re-running adds no duplicate jobs or sources.
        res2 = rjh.discover_all(cfg, force=True)
        self.assertEqual(res2["added_jobs"], 0)
        self.assertEqual(
            len([s for s in cfg["sources"] if "(discovered)" in s.get("name", "")]), 3)

    def test_collect_discovered_requires_verified(self):
        key = rjh.canonicalize_url("https://eures.europa.eu")
        rjh._save_catalog_feed({"url": key, "name": "X", "feed_url": "", "feed_type": "",
                                "method": "probe", "status": "none", "detail": "",
                                "item_count": 0, "meta": "",
                                "checked_at": rjh.dt.datetime.now().isoformat()})
        self.assertEqual(rjh.collect_discovered_source(key, {"sources": []}, {}), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
