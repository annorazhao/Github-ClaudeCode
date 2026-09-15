#!/usr/bin/env python3
"""Offline tests for news_digest.py. Feeds are generated on the fly with fresh
timestamps and read from disk, so no network is needed.

    python3 -m unittest scripts/news_digest/test_news_digest.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_digest as nd  # noqa: E402

NOW = datetime(2026, 9, 15, 14, 5, tzinfo=timezone.utc)


def rss(items: list[tuple[str, str, datetime, str]], title="Test feed") -> str:
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><pubDate>{format_datetime(dt)}</pubDate>"
        f"<description><![CDATA[{desc}]]></description></item>"
        for t, link, dt, desc in items
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>{title}</title>{body}</channel></rss>"


def atom(items: list[tuple[str, str, datetime, str]], title="Atom feed") -> str:
    body = "".join(
        f"<entry><title>{t}</title><link href='{link}'/><id>{link}</id>"
        f"<updated>{dt.isoformat()}</updated><summary>{desc}</summary></entry>"
        for t, link, dt, desc in items
    )
    return (f"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>"
            f"<title>{title}</title>{body}</feed>")


CONFIG_TEMPLATE = """
[settings]
lookback_hours = 26
max_items_per_topic = 3
max_candidates_per_topic = 10
timezone = "America/New_York"

[topics.transportation]
label = "Transportation"
keywords = ["transit", "freight", "rail", "EV", "highway", "port"]

[topics.energy]
label = "Energy"
keywords = ["electricity", "grid", "solar", "natural gas", "FERC"]

[topics.industrial_organization]
label = "Industrial Organization & Antitrust"
keywords = ["antitrust", "merger", "FTC", "monopoly"]

[[feed]]
name = "Transit Wire"
url = "{transit}"
topic = "transportation"

[[feed]]
name = "General Business"
url = "{general}"
topic = "auto"
weight = 0.9

[[feed]]
name = "Broken"
url = "{broken}"
topic = "energy"

[[feed]]
name = "Missing"
url = "{missing}"
topic = "energy"
"""


class Fixture:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        fresh = NOW - timedelta(hours=3)
        older = NOW - timedelta(hours=20)
        stale = NOW - timedelta(hours=40)
        (d / "transit.xml").write_text(rss([
            ("Transit agency expands rail service", "https://ex.org/rail?utm_source=x", fresh,
             "<p>The agency added <b>rail</b> frequency on two lines.</p>"),
            ("Freight volumes rise at the port", "https://ex.org/port", older, "Port freight up 4 percent."),
            ("Old story about highways", "https://ex.org/old", stale, "Should be excluded by the window."),
            ("Duplicate of the rail story", "https://ex.org/rail/", fresh, "Same URL modulo tracking params."),
        ]))
        (d / "general.xml").write_text(atom([
            ("FTC challenges hospital merger", "https://biz.example/ftc", fresh,
             "The FTC sued to block a merger of two hospital systems, citing antitrust concerns."),
            ("Grid operator warns of tight electricity supply", "https://biz.example/grid", fresh,
             "Natural gas prices and solar output shape the outlook."),
            ("A cop directed traffic downtown", "https://biz.example/cop", fresh,
             "Nothing here about our topics; the lowercase word cop must not match COP."),
            ("Quarterly earnings roundup", "https://biz.example/earnings", fresh, "Generic business story."),
        ]))
        # An HTML error page where a feed used to be: no entries, so it must be reported, not
        # silently treated as an empty feed. (feedparser salvages *truncated* XML, which is fine.)
        (d / "broken.xml").write_text("<html><body><h1>503 Service Unavailable</h1></body></html>")
        self.config_path = d / "feeds.toml"
        self.config_path.write_text(CONFIG_TEMPLATE.format(
            transit=d / "transit.xml", general=d / "general.xml", broken=d / "broken.xml",
            missing=d / "does-not-exist.xml"))

    def cleanup(self):
        self.dir.cleanup()


class TextUtilsTests(unittest.TestCase):
    def test_clean_text_strips_html_and_truncates(self):
        self.assertEqual(nd.clean_text("<p>Hello &amp;   <b>world</b></p>"), "Hello & world")
        long = "Sentence one. " * 100
        out = nd.clean_text(long, limit=80)
        self.assertLessEqual(len(out), 84)
        self.assertTrue(out.endswith("…"))

    def test_canonical_url_drops_tracking_and_trailing_slash(self):
        a = nd.canonical_url("https://Ex.org/rail/?utm_source=x&id=2#frag")
        b = nd.canonical_url("https://ex.org/rail?id=2")
        self.assertEqual(a, b)

    def test_keyword_matching_respects_word_boundaries_and_acronym_case(self):
        pats = nd.compile_keywords(["port", "COP", "EV", "natural gas"])
        self.assertEqual(nd.keyword_hits("The quarterly report is out.", pats), 0)
        self.assertEqual(nd.keyword_hits("Freight at the port rose.", pats), 1)
        self.assertEqual(nd.keyword_hits("A cop directed traffic.", pats), 0)
        self.assertEqual(nd.keyword_hits("Delegates arrive for COP talks.", pats), 1)
        self.assertEqual(nd.keyword_hits("Every EV needs a charger; ev is not a word.", pats), 1)
        self.assertEqual(nd.keyword_hits("Natural Gas prices fell.", pats), 1)

    def test_should_run_now_uses_local_hour(self):
        at_10_edt = datetime(2026, 9, 15, 14, 20, tzinfo=timezone.utc)   # 10:20 EDT
        at_10_est = datetime(2026, 12, 15, 15, 5, tzinfo=timezone.utc)   # 10:05 EST
        self.assertTrue(nd.should_run_now(10, "America/New_York", at_10_edt))
        self.assertFalse(nd.should_run_now(10, "America/New_York", at_10_est - timedelta(hours=1)))
        self.assertTrue(nd.should_run_now(10, "America/New_York", at_10_est))


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)
        self.collected = nd.collect(self.cfg, now=NOW)

    def tearDown(self):
        self.fx.cleanup()

    def items(self, topic):
        return next(t for t in self.collected.topics if t["key"] == topic)["items"]

    def test_window_dedupe_and_fixed_topic(self):
        titles = [i["title"] for i in self.items("transportation")]
        self.assertIn("Transit agency expands rail service", titles)
        self.assertIn("Freight volumes rise at the port", titles)
        self.assertNotIn("Old story about highways", titles)
        self.assertNotIn("Duplicate of the rail story", titles)
        rail = next(i for i in self.items("transportation") if "rail" in i["title"])
        self.assertEqual(rail["url"], "https://ex.org/rail?utm_source=x")  # original link kept
        self.assertEqual(rail["source"], "Transit Wire")

    def test_auto_classification(self):
        io_titles = [i["title"] for i in self.items("industrial_organization")]
        energy_titles = [i["title"] for i in self.items("energy")]
        self.assertEqual(io_titles, ["FTC challenges hospital merger"])
        self.assertEqual(energy_titles, ["Grid operator warns of tight electricity supply"])
        all_titles = [i["title"] for t in self.collected.topics for i in t["items"]]
        self.assertNotIn("A cop directed traffic downtown", all_titles)
        self.assertNotIn("Quarterly earnings roundup", all_titles)

    def test_feed_health_and_stats(self):
        status = {h["name"]: h["status"] for h in self.collected.feed_health}
        self.assertEqual(status["Transit Wire"], "ok")
        self.assertEqual(status["General Business"], "ok")
        self.assertEqual(status["Broken"], "parse-error")
        self.assertEqual(status["Missing"], "error")
        self.assertEqual(self.collected.stats["feeds_ok"], 2)
        self.assertEqual(self.collected.stats["feeds"], 4)
        # ok feeds sort first, then alphabetical
        self.assertEqual([h["status"] for h in self.collected.feed_health][:2], ["ok", "ok"])

    def test_scores_favor_recent_and_keyword_rich(self):
        rail = next(i for i in self.items("transportation") if "rail" in i["title"])
        port = next(i for i in self.items("transportation") if "port" in i["title"])
        self.assertGreater(rail["score"], port["score"])

    def test_collect_round_trips_through_json(self):
        payload = json.loads(json.dumps(nd.asdict(self.collected)))
        self.assertEqual(payload["stats"]["items_kept"], 4)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)
        self.collected = nd.collect(self.cfg, now=NOW)

    def tearDown(self):
        self.fx.cleanup()

    def test_fallback_digest_renders_all_formats(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            digest = nd.build_digest(self.collected, self.cfg)
        self.assertEqual(digest["generated_by"], "keyword-fallback")
        self.assertEqual(digest["date"], "2026-09-15")
        html_out = nd.render_html(digest, self.cfg)
        text_out = nd.render_text(digest, self.cfg)
        md_out = nd.render_markdown(digest, self.cfg)
        for out in (html_out, text_out, md_out):
            self.assertIn("Transit agency expands rail service", out)
            self.assertIn("FTC challenges hospital merger", out)
            self.assertIn("industrial organization", out.lower())   # text output upper-cases labels
        self.assertIn("href='https://ex.org/rail?utm_source=x'", html_out)
        self.assertIn("Broken (parse-error", html_out)
        self.assertIn("2 of 4 feeds", html_out)
        subject = nd.subject_line(digest, nd.ZoneInfo("America/New_York"))
        self.assertTrue(subject.startswith("News digest Tuesday, September 15, 2026: "))
        self.assertIn("Industrial Organization", subject)
        self.assertNotIn("&", subject)

    def test_html_escapes_untrusted_feed_text(self):
        digest = nd.summarize_fallback(self.collected, self.cfg)
        digest["sections"][0]["items"][0]["headline"] = "<script>alert(1)</script>"
        digest["date"] = "2026-09-15"
        out = nd.render_html(digest, self.cfg)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_map_claude_output_resolves_ids_and_drops_unknown(self):
        rail = next(i for t in self.collected.topics for i in t["items"] if "rail" in i["title"])
        ftc = next(i for t in self.collected.topics for i in t["items"] if "FTC" in i["title"])
        data = {
            "top_line": "A day of rail and mergers.",
            "sections": [
                {"topic": "transportation", "items": [
                    {"id": rail["id"], "headline": "Rail service expands", "summary": "More trains.", "why_it_matters": "Capacity."},
                    {"id": "not-a-real-id", "headline": "Invented", "summary": "x", "why_it_matters": ""},
                ]},
                {"topic": "industrial_organization", "items": [
                    {"id": ftc["id"], "headline": "", "summary": "", "why_it_matters": ""},
                ]},
            ],
        }
        digest = nd.map_claude_output(data, self.collected, self.cfg)
        t_items = next(s for s in digest["sections"] if s["topic"] == "transportation")["items"]
        self.assertEqual(len(t_items), 1)
        self.assertEqual(t_items[0]["url"], rail["url"])
        self.assertEqual(t_items[0]["headline"], "Rail service expands")
        io_items = next(s for s in digest["sections"] if s["topic"] == "industrial_organization")["items"]
        self.assertEqual(io_items[0]["headline"], ftc["title"])   # empty headline falls back to title
        self.assertEqual(digest["top_line"], "A day of rail and mergers.")
        labels = [s["label"] for s in digest["sections"]]
        self.assertEqual(labels, ["Transportation", "Energy", "Industrial Organization & Antitrust"])

    def test_validate_digest_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            nd.validate_digest({"sections": [{"topic": "x", "items": [{"headline": "no url"}]}]})
        with self.assertRaises(ValueError):
            nd.validate_digest([])


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", msg, from_addr, to_addrs))


class EmailTests(unittest.TestCase):
    ENV = {
        "SMTP_HOST": "smtp.example.edu", "SMTP_PORT": "587", "SMTP_USERNAME": "me@example.edu",
        "SMTP_PASSWORD": "app-password", "DIGEST_TO": "me@example.edu, other@example.org",
    }

    def test_settings_from_env(self):
        s = nd.smtp_settings_from_env(self.ENV)
        self.assertEqual(s["security"], "starttls")
        self.assertEqual(s["recipients"], ["me@example.edu", "other@example.org"])
        self.assertEqual(s["sender"], "me@example.edu")
        s465 = nd.smtp_settings_from_env({**self.ENV, "SMTP_PORT": "465"})
        self.assertEqual(s465["security"], "ssl")
        with self.assertRaises(ValueError):
            nd.smtp_settings_from_env({k: v for k, v in self.ENV.items() if k != "DIGEST_TO"})

    def test_send_email_does_starttls_login_and_send(self):
        FakeSMTP.instances.clear()
        settings = nd.smtp_settings_from_env(self.ENV)
        msg = nd.build_message("Subj", "text body", "<p>html body</p>", settings["sender"], settings["recipients"])
        nd.send_email(msg, settings, smtp_factory=FakeSMTP)
        smtp = FakeSMTP.instances[-1]
        kinds = [c[0] for c in smtp.calls]
        self.assertEqual(kinds, ["ehlo", "starttls", "ehlo", "login", "send", "quit"])
        sent = next(c for c in smtp.calls if c[0] == "send")
        self.assertEqual(sent[3], ["me@example.edu", "other@example.org"])
        self.assertEqual(sent[1]["Subject"], "Subj")
        parts = [p.get_content_type() for p in sent[1].iter_parts()]
        self.assertEqual(parts, ["text/plain", "text/html"])

    def test_deliver_dry_run_writes_files_and_never_sends(self):
        fx = Fixture()
        try:
            cfg = nd.load_config(fx.config_path)
            collected = nd.collect(cfg, now=NOW)
            digest = nd.build_digest(collected, cfg, use_ai=False)
            out = Path(fx.dir.name) / "out" / "digest.html"
            with mock.patch.object(nd, "send_email") as send:
                rc = nd.deliver(digest, cfg, str(out), dry_run=True)
            self.assertEqual(rc, 0)
            send.assert_not_called()
            self.assertTrue(out.exists())
            self.assertTrue(out.with_suffix(".txt").exists())
            self.assertTrue(out.with_suffix(".md").exists())
        finally:
            fx.cleanup()

    def test_deliver_without_smtp_settings_is_a_config_error(self):
        fx = Fixture()
        try:
            cfg = nd.load_config(fx.config_path)
            digest = nd.build_digest(nd.collect(cfg, now=NOW), cfg, use_ai=False)
            with mock.patch.dict(os.environ, {}, clear=True):
                rc = nd.deliver(digest, cfg, None, dry_run=False)
            self.assertEqual(rc, 2)
        finally:
            fx.cleanup()


class CliTests(unittest.TestCase):
    def test_collect_send_round_trip_via_cli(self):
        fx = Fixture()
        try:
            d = Path(fx.dir.name)
            items = d / "items.json"
            rc = nd.main(["collect", "--config", str(fx.config_path), "--out", str(items)])
            self.assertEqual(rc, 0)
            payload = json.loads(items.read_text())
            self.assertGreater(payload["stats"]["items_kept"], 0)
            # A digest a Claude session would write from the collected items:
            first = payload["topics"][0]["items"][0]
            digest = {"top_line": "Test.", "sections": [{"topic": "transportation", "items": [
                {"headline": first["title"], "url": first["url"], "source": first["source"],
                 "published": first["published"], "summary": "S.", "why_it_matters": ""}]}]}
            dpath = d / "digest.json"
            dpath.write_text(json.dumps(digest))
            out = d / "digest.html"
            rc = nd.main(["send", "--config", str(fx.config_path), "--digest", str(dpath),
                          "--out", str(out), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn(first["title"], out.read_text())
            self.assertIn("Transportation", out.read_text())     # label filled from config
        finally:
            fx.cleanup()

    def test_run_all_feeds_dead_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "feeds.toml"
            cfg.write_text(CONFIG_TEMPLATE.format(transit=Path(td) / "a", general=Path(td) / "b",
                                                  broken=Path(td) / "c", missing=Path(td) / "d"))
            rc = nd.main(["run", "--config", str(cfg), "--dry-run"])
            self.assertEqual(rc, 3)

    def test_only_at_hour_skips_quietly(self):
        fx = Fixture()
        try:
            with mock.patch.object(nd, "should_run_now", return_value=False):
                rc = nd.main(["run", "--config", str(fx.config_path), "--dry-run", "--only-at-hour", "10"])
            self.assertEqual(rc, 0)
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main()
