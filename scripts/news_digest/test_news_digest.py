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
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_digest as nd  # noqa: E402

NOW = datetime(2026, 9, 15, 14, 5, tzinfo=timezone.utc)
TODAY = date(2026, 9, 15)


def rss(items: list[tuple[str, str, datetime, str]], title="Test feed", source: str | None = None) -> str:
    src = f"<source url='https://x'>{source}</source>" if source else ""
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><pubDate>{format_datetime(dt)}</pubDate>"
        f"<description><![CDATA[{desc}]]></description>{src}</item>"
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
max_candidates_per_section = 10
timezone = "America/New_York"
traffic_keywords = ["traffic", "toll", "tolls", "transit", "bus", "lanes", "crash", "commute", "Metrorail", "Metro"]

[topics.transportation]
label = "Transportation"
keywords = ["transit", "freight", "rail", "EV", "highway", "port", "toll", "transportation"]

[topics.energy]
label = "Energy"
keywords = ["electricity", "grid", "solar", "natural gas", "FERC"]

[topics.industrial_organization]
label = "Industrial Organization & Antitrust"
keywords = ["antitrust", "merger", "FTC", "monopoly"]

[regions.nova]
label = "Northern Virginia"
boost = 1.6
keywords = ["Fairfax County", "Arlington", "Loudoun", "Alexandria", "I-66", "VDOT", "Virginia", "Herndon"]
exclude = ["West Virginia", "Virginia Beach"]

[regions.dc]
label = "District of Columbia"
boost = 1.3
keywords = ["DDOT", "D.C.", "DC"]

[regions.maryland]
label = "Maryland"
boost = 1.3
keywords = ["Maryland", "Montgomery County", "Purple Line"]

[regions.regional]
label = "Region-wide and Metro"
boost = 1.3
keywords = ["WMATA", "Metrorail"]

[[jurisdiction]]
label = "Town of Herndon"
level = "town"
region = "nova"
keywords = ["Herndon"]

[[jurisdiction]]
label = "Fairfax County"
level = "county"
region = "nova"
keywords = ["Fairfax County", "Herndon"]

[[jurisdiction]]
label = "Virginia"
level = "state"
region = "nova"
keywords = ["VDOT", "Virginia"]

[[jurisdiction]]
label = "WMATA"
level = "regional"
region = "regional"
keywords = ["WMATA", "Metrorail"]

[limits]
dc_nova = 4
us_default = 3
world = 2

[archive]
enabled = true
anchor = "2026-09-16"
landmark_days = 2
start_week = "2016-01-04"
weeks_per_day = 1
order = "forward"
max_items = 8
landmarks_file = "{landmarks}"
query = "(Fairfax OR Arlington) (traffic OR toll)"

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
name = "Local Paper"
url = "{local}"
topic = "auto"
region = "dc"
subregion = "nova"

[[feed]]
name = "Google News: test"
url = "{gnews}"
topic = "auto"
region = "dc"
subregion = "regional"

[[feed]]
name = "Overseas"
url = "{world}"
topic = "auto"
region = "world"

[[feed]]
name = "Broken"
url = "{broken}"
topic = "energy"

[[feed]]
name = "Missing"
url = "{missing}"
topic = "energy"
"""

LANDMARKS = """
[[landmark]]
date = "2012-11-17"
tier = 1
title = "The 495 Express Lanes open"
jurisdiction = "Fairfax County"
level = "state"
summary = "Priced lanes opened on the Beltway."
query = "495 Express Lanes open"

[[landmark]]
date = "2016-01-06"
tier = 2
title = "A landmark inside the first archive week"
jurisdiction = "Virginia"
level = "state"
confidence = "medium"
summary = "Something happened in early January 2016."
query = "January 2016 Virginia transportation"

[[landmark]]
date = "2017-12-04"
tier = 1
title = "Tolling begins on I-66 inside the Beltway"
jurisdiction = "Arlington County"
level = "state"
summary = "Dynamic tolls started."
query = "I-66 tolls begin"

[[landmark]]
date = "2020-09"
tier = 2
title = "A month-precision landmark"
summary = "Month only."
query = "month only"

[[landmark]]
date = "2024-09-14"
tier = 3
title = "Anniversary item near mid-September"
summary = "Two years before the test date."
query = "anniversary"
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
            ("VDOT opens new I-66 toll lanes in Fairfax County", "https://ex.org/i66", fresh,
             "Virginia's VDOT opened express lanes; tolls vary."),
        ]))
        (d / "general.xml").write_text(atom([
            ("FTC challenges hospital merger", "https://biz.example/ftc", fresh,
             "The FTC sued to block a merger of two hospital systems, citing antitrust concerns."),
            ("Grid operator warns of tight electricity supply", "https://biz.example/grid", fresh,
             "Natural gas prices and solar output shape the outlook."),
            ("Maryland utility files electricity rate case", "https://biz.example/mdrate", fresh,
             "A Maryland utility asked regulators to raise electricity rates."),
            ("A cop directed traffic downtown", "https://biz.example/cop", fresh,
             "Nothing here about our topics; the lowercase word cop must not match COP."),
            ("Quarterly earnings roundup", "https://biz.example/earnings", fresh, "Generic business story."),
            ("West Virginia highway crash closes lanes", "https://biz.example/wv", fresh,
             "A crash on a West Virginia highway closed lanes for hours."),
        ]))
        (d / "local.xml").write_text(rss([
            ("Herndon council adds bus shelters on Elden Street", "https://local.example/herndon", fresh,
             "The town council approved new transit shelters."),
            ("County budget hearing draws crowd", "https://local.example/budget", fresh,
             "Residents spoke about schools and taxes."),
            ("Council debates toll relief", "https://local.example/toll", fresh,
             "Members discussed commute costs and tolls."),
        ], title="Local Paper"))
        (d / "gnews.xml").write_text(rss([
            ("WMATA approves Metrorail service plan - The Washington Post", "https://news.google.com/rss/articles/abc",
             fresh, "Metrorail service changes were approved by the WMATA board."),
        ], title="Google News", source="The Washington Post"))
        (d / "world.xml").write_text(rss([
            ("EU opens antitrust probe into cloud providers", "https://world.example/eu", fresh,
             "The European Commission opened an antitrust investigation."),
            ("Rain expected in London", "https://world.example/weather", fresh, "Umbrellas advised."),
        ], title="Overseas"))
        (d / "broken.xml").write_text("<html><body><h1>503 Service Unavailable</h1></body></html>")
        (d / "archive.toml").write_text(LANDMARKS)
        self.config_path = d / "feeds.toml"
        self.config_path.write_text(CONFIG_TEMPLATE.format(
            transit=d / "transit.xml", general=d / "general.xml", local=d / "local.xml",
            gnews=d / "gnews.xml", world=d / "world.xml", broken=d / "broken.xml",
            missing=d / "does-not-exist.xml", landmarks=d / "archive.toml"))

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
        pats = nd.compile_keywords(["port", "COP", "EV", "natural gas", "DC"])
        self.assertEqual(nd.keyword_hits("The quarterly report is out.", pats), 0)
        self.assertEqual(nd.keyword_hits("Freight at the port rose.", pats), 1)
        self.assertEqual(nd.keyword_hits("A cop directed traffic.", pats), 0)
        self.assertEqual(nd.keyword_hits("Delegates arrive for COP talks.", pats), 1)
        self.assertEqual(nd.keyword_hits("Every EV needs a charger; ev is not a word.", pats), 1)
        self.assertEqual(nd.keyword_hits("Natural Gas prices fell.", pats), 1)
        self.assertEqual(nd.keyword_hits("Traffic in DC vs. the dc motor.", pats), 1)

    def test_should_run_now_uses_local_hour(self):
        at_10_edt = datetime(2026, 9, 15, 14, 20, tzinfo=timezone.utc)   # 10:20 EDT
        at_10_est = datetime(2026, 12, 15, 15, 5, tzinfo=timezone.utc)   # 10:05 EST
        self.assertTrue(nd.should_run_now(10, "America/New_York", at_10_edt))
        self.assertFalse(nd.should_run_now(10, "America/New_York", at_10_est - timedelta(hours=1)))
        self.assertTrue(nd.should_run_now(10, "America/New_York", at_10_est))

    def test_google_news_url_encodes_query(self):
        url = nd.google_news_url('("Fairfax County") traffic when:2d')
        self.assertTrue(url.startswith("https://news.google.com/rss/search?q="))
        self.assertIn("Fairfax", url)
        self.assertIn("hl=en-US", url)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)

    def tearDown(self):
        self.fx.cleanup()

    def classify(self, text, feed_region="us", topic="auto", subregion=None):
        feed = {"topic": topic, "region": feed_region}
        if subregion:
            feed["subregion"] = subregion
        return nd.classify_item(feed, self.cfg, text)

    def test_national_feed_item_about_nova_transport_goes_to_dc_nova(self):
        v = self.classify("VDOT opens new I-66 toll lanes in Fairfax County. Tolls vary.")
        self.assertEqual(v["section"], "dc_nova")
        self.assertEqual(v["topic"], "transportation")
        self.assertAlmostEqual(v["boost"], 1.6)

    def test_region_mention_without_transport_stays_in_us_topic(self):
        v = self.classify("Maryland utility files electricity rate case. Regulators will review rates.")
        self.assertEqual(v["section"], "us_energy")

    def test_exclude_keywords_keep_west_virginia_out_of_nova(self):
        v = self.classify("West Virginia highway crash closes lanes. A crash closed lanes for hours.")
        self.assertIsNotNone(v)
        self.assertEqual(v["section"], "us_transportation")

    def test_local_feed_defaults_to_subregion_and_drops_non_traffic(self):
        v = self.classify("Council debates toll relief. Members discussed commute costs.", "dc", subregion="nova")
        self.assertEqual(v["section"], "dc_nova")
        self.assertIsNone(self.classify("County budget hearing draws crowd. Schools and taxes.", "dc", subregion="nova"))

    def test_world_feed_needs_a_topic(self):
        self.assertEqual(self.classify("EU opens antitrust probe into cloud providers.", "world")["section"], "world")
        self.assertIsNone(self.classify("Rain expected in London. Umbrellas advised.", "world"))

    def test_jurisdiction_prefers_most_specific(self):
        label, level = nd.detect_jurisdiction("Herndon council adds bus shelters in Fairfax County", self.cfg, "nova")
        self.assertEqual((label, level), ("Town of Herndon", "town"))    # most specific level wins
        label, level = nd.detect_jurisdiction("VDOT repaves Virginia roads in Fairfax County", self.cfg, "nova")
        self.assertEqual((label, level), ("Fairfax County", "county"))   # county beats state despite fewer hits
        label, level = nd.detect_jurisdiction("VDOT announces Virginia statewide plan", self.cfg, "nova")
        self.assertEqual((label, level), ("Virginia", "state"))
        self.assertEqual(nd.detect_jurisdiction("WMATA board meets", self.cfg, "nova"), ("", ""))

    def test_section_plan_order_and_caps(self):
        keys = [p["key"] for p in nd.section_plan(self.cfg)]
        self.assertEqual(keys, ["dc_nova", "dc_dc", "dc_maryland", "dc_regional", "us_transportation",
                                "us_energy", "us_industrial_organization", "world", "archive"])
        self.assertEqual(nd.cap_for(self.cfg, "dc_nova"), 4)
        self.assertEqual(nd.cap_for(self.cfg, "dc_dc"), nd.DEFAULT_DC_LIMIT)
        self.assertEqual(nd.cap_for(self.cfg, "us_energy"), 3)
        self.assertEqual(nd.cap_for(self.cfg, "world"), 2)
        self.assertEqual(nd.cap_for(self.cfg, "archive"), 8)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)
        self.collected = nd.collect(self.cfg, now=NOW)

    def tearDown(self):
        self.fx.cleanup()

    def items(self, key):
        return next(s for s in self.collected.sections if s["key"] == key)["items"]

    def test_window_dedupe_and_sections(self):
        us_t = [i["title"] for i in self.items("us_transportation")]
        self.assertIn("Transit agency expands rail service", us_t)
        self.assertIn("Freight volumes rise at the port", us_t)
        self.assertIn("West Virginia highway crash closes lanes", us_t)
        self.assertNotIn("Old story about highways", us_t)
        self.assertNotIn("Duplicate of the rail story", us_t)
        nova = [i["title"] for i in self.items("dc_nova")]
        self.assertIn("VDOT opens new I-66 toll lanes in Fairfax County", nova)
        self.assertIn("Herndon council adds bus shelters on Elden Street", nova)
        self.assertIn("Council debates toll relief", nova)
        self.assertEqual([i["title"] for i in self.items("us_industrial_organization")], ["FTC challenges hospital merger"])
        self.assertEqual(sorted(i["title"] for i in self.items("us_energy")),
                         ["Grid operator warns of tight electricity supply", "Maryland utility files electricity rate case"])
        self.assertEqual([i["title"] for i in self.items("world")], ["EU opens antitrust probe into cloud providers"])
        everything = [i["title"] for s in self.collected.sections for i in s["items"]]
        self.assertNotIn("A cop directed traffic downtown", everything)
        self.assertNotIn("County budget hearing draws crowd", everything)

    def test_google_news_source_and_title_cleanup(self):
        regional = self.items("dc_regional")
        self.assertEqual(len(regional), 1)
        self.assertEqual(regional[0]["title"], "WMATA approves Metrorail service plan")
        self.assertEqual(regional[0]["source"], "The Washington Post")
        self.assertEqual(regional[0]["jurisdiction"], "WMATA")

    def test_jurisdiction_tags_on_nova_items(self):
        by_title = {i["title"]: i for i in self.items("dc_nova")}
        self.assertEqual(by_title["Herndon council adds bus shelters on Elden Street"]["jurisdiction"], "Town of Herndon")
        self.assertEqual(by_title["VDOT opens new I-66 toll lanes in Fairfax County"]["jurisdiction"], "Fairfax County")
        self.assertEqual(by_title["Council debates toll relief"]["jurisdiction"], "")

    def test_region_boost_ranks_nova_above_generic(self):
        nova = self.items("dc_nova")
        self.assertEqual(nova[0]["title"], "VDOT opens new I-66 toll lanes in Fairfax County")
        self.assertGreater(nova[0]["score"], self.items("us_transportation")[0]["score"])

    def test_feed_health_and_stats(self):
        status = {h["name"]: h["status"] for h in self.collected.feed_health}
        self.assertEqual(status["Transit Wire"], "ok")
        self.assertEqual(status["Local Paper"], "ok")
        self.assertEqual(status["Broken"], "parse-error")
        self.assertEqual(status["Missing"], "error")
        self.assertEqual(self.collected.stats["feeds_ok"], 5)
        self.assertEqual([h["status"] for h in self.collected.feed_health][:5], ["ok"] * 5)
        self.assertNotIn("Google News archive", " ".join(status))   # landmark phase: no archive fetch

    def test_archive_plan_in_collected(self):
        self.assertEqual(self.collected.archive["phase"], "off")   # NOW is the day before the anchor
        later = nd.collect(self.cfg, now=NOW + timedelta(days=1))
        self.assertEqual(later.archive["phase"], "landmarks")
        self.assertEqual([l["headline"] for l in later.archive["landmark_items"]], ["The 495 Express Lanes open"])
        self.assertIn("cd_min:10/27/2012", later.archive["landmark_items"][0]["coverage_url"])

    def test_collect_round_trips_through_json(self):
        payload = json.loads(json.dumps(nd.asdict(self.collected)))
        self.assertGreater(payload["stats"]["items_kept"], 5)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)
        self.landmarks = nd.load_landmarks(self.cfg)

    def tearDown(self):
        self.fx.cleanup()

    def test_landmarks_parse_dates_and_precision(self):
        by_title = {l["title"]: l for l in self.landmarks}
        self.assertEqual(by_title["A month-precision landmark"]["precision"], "month")
        self.assertEqual(by_title["A month-precision landmark"]["_date"], date(2020, 9, 15))
        self.assertEqual(nd.landmark_date_label(by_title["A month-precision landmark"]), "September 2020")
        self.assertEqual(nd.landmark_date_label(by_title["The 495 Express Lanes open"]), "November 17, 2012")
        self.assertEqual([l["title"] for l in self.landmarks][0], "The 495 Express Lanes open")   # sorted by date

    def test_schedule_phases(self):
        anchor = date(2026, 9, 16)
        p0 = nd.archive_plan(self.cfg, anchor, self.landmarks)
        self.assertEqual((p0["phase"], p0["part"], p0["of"]), ("landmarks", 1, 2))
        self.assertEqual([l["title"] for l in p0["landmarks"]], ["The 495 Express Lanes open"])
        p1 = nd.archive_plan(self.cfg, anchor + timedelta(days=1), self.landmarks)
        self.assertEqual([l["title"] for l in p1["landmarks"]], ["Tolling begins on I-66 inside the Beltway"])
        p2 = nd.archive_plan(self.cfg, anchor + timedelta(days=2), self.landmarks)
        self.assertEqual(p2["phase"], "week")
        self.assertEqual(p2["week_start"], "2016-01-04")
        self.assertEqual(p2["index"], 1)
        self.assertEqual([l["title"] for l in p2["landmarks"]], ["A landmark inside the first archive week"])
        p3 = nd.archive_plan(self.cfg, anchor + timedelta(days=3), self.landmarks)
        self.assertEqual(p3["week_start"], "2016-01-11")
        self.assertEqual(nd.archive_plan(self.cfg, anchor - timedelta(days=1), self.landmarks)["phase"], "off")

    def test_schedule_reaches_present_and_switches_to_anniversaries(self):
        p = nd.archive_plan(self.cfg, TODAY, self.landmarks, day_override=99999)   # past every available week
        self.assertEqual(p["phase"], "anniversary")
        self.assertEqual([l["title"] for l in p["landmarks"]], ["Anniversary item near mid-September"])
        p_mid_sept = nd.archive_plan(self.cfg, date(2040, 9, 15), self.landmarks, day_override=99999)
        self.assertEqual([l["title"] for l in p_mid_sept["landmarks"]], ["Anniversary item near mid-September"])

    def test_overrides(self):
        p = nd.archive_plan(self.cfg, TODAY, self.landmarks, week_override="2017-12-06")   # a Wednesday
        self.assertEqual(p["week_start"], "2017-12-04")
        self.assertEqual([l["title"] for l in p["landmarks"]], ["Tolling begins on I-66 inside the Beltway"])
        p = nd.archive_plan(self.cfg, TODAY, self.landmarks, day_override=1)
        self.assertEqual(p["part"], 2)

    def test_backward_order(self):
        self.cfg["archive"]["order"] = "backward"
        p = nd.archive_plan(self.cfg, date(2026, 9, 18), self.landmarks)
        self.assertEqual(p["week_start"], "2026-09-07")     # last full week before Sept 18, 2026

    def test_archive_collect_uses_dated_google_news_query(self):
        plan = nd.archive_plan(self.cfg, date(2026, 9, 18), self.landmarks)
        captured = {}

        def fake_parse(feed_cfg, cfg, now, window_start):
            captured.update(url=feed_cfg["url"], now=now, window_start=window_start)
            item = nd.Item(id="x", title="Old toll story", url="https://old", source="WTOP", topic="transportation",
                           section="dc_nova", published="2016-01-05T12:00:00+00:00", summary="s", score=1.0)
            return [item], nd.FeedHealth(name=feed_cfg["name"], url=feed_cfg["url"], topic="auto", status="ok")

        with mock.patch.object(nd, "parse_feed", side_effect=fake_parse):
            items, health = nd.archive_collect(self.cfg, plan, NOW)
        self.assertIn("after%3A2016-01-04", captured["url"])
        self.assertIn("before%3A2016-01-11", captured["url"])
        self.assertEqual(captured["window_start"].date(), date(2016, 1, 4))
        self.assertEqual(items[0].section, "archive")
        self.assertEqual(health[0].items_kept, 1)

    def test_coverage_url_windows(self):
        u = nd.coverage_search_url("q", date(2020, 9, 15), "month")
        self.assertIn("cd_min:08/22/2020", u)
        self.assertIn("cd_max:10/12/2020", u)
        u = nd.coverage_search_url("q", date(2025, 7, 1), "year")
        self.assertIn("cd_min:01/01/2025,cd_max:12/31/2025", u)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cfg = nd.load_config(self.fx.config_path)
        self.collected = nd.collect(self.cfg, now=NOW, archive_day=0)   # landmarks part 1

    def tearDown(self):
        self.fx.cleanup()

    def test_fallback_digest_renders_all_formats_with_groups_and_landmarks(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            digest = nd.build_digest(self.collected, self.cfg)
        self.assertEqual(digest["generated_by"], "keyword-fallback")
        self.assertEqual(digest["date"], "2026-09-15")
        archive = next(s for s in digest["sections"] if s["section"] == "archive")
        self.assertEqual(archive["items"][0]["kind"], "landmark")
        self.assertTrue(archive["title"].startswith("Landmarks, part 1 of 2"))
        html_out = nd.render_html(digest, self.cfg)
        text_out = nd.render_text(digest, self.cfg)
        md_out = nd.render_markdown(digest, self.cfg)
        for out in (html_out, text_out, md_out):
            self.assertIn("VDOT opens new I-66 toll lanes in Fairfax County", out)
            self.assertIn("FTC challenges hospital merger", out)
            self.assertIn("The 495 Express Lanes open", out)
            self.assertIn("washington region", out.lower())   # text output upper-cases group headers
        self.assertIn("Fairfax County (county)", html_out)
        self.assertIn("Search contemporary coverage", html_out)
        self.assertIn("cd_min:10/27/2012", html_out)
        self.assertIn("Broken (parse-error", html_out)
        # groups appear in order: dc, us, world, archive
        self.assertLess(html_out.index("Washington region"), html_out.index("United States"))
        self.assertLess(html_out.index("United States"), html_out.index("From the archive"))
        subject = nd.subject_line(digest, nd.ZoneInfo("America/New_York"))
        self.assertTrue(subject.startswith("News digest Tuesday, September 15, 2026: "))
        self.assertIn("DC-region", subject)
        self.assertIn("archive: Landmarks, part 1 of 2", subject)

    def test_html_escapes_untrusted_feed_text(self):
        digest = nd.summarize_fallback(self.collected, self.cfg)
        digest["sections"][0]["items"][0]["headline"] = "<script>alert(1)</script>"
        digest["date"] = "2026-09-15"
        out = nd.render_html(digest, self.cfg)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_map_claude_output_resolves_ids_and_drops_unknown(self):
        i66 = next(i for s in self.collected.sections for i in s["items"] if "I-66" in i["title"])
        ftc = next(i for s in self.collected.sections for i in s["items"] if "FTC" in i["title"])
        data = {
            "top_line": "A day of tolls and mergers.",
            "sections": [
                {"section": "dc_nova", "items": [
                    {"id": i66["id"], "headline": "I-66 lanes open", "summary": "Lanes.", "why_it_matters": "Pricing."},
                    {"id": "not-a-real-id", "headline": "Invented", "summary": "x", "why_it_matters": ""},
                ]},
                {"section": "us_industrial_organization", "items": [
                    {"id": ftc["id"], "headline": "", "summary": "", "why_it_matters": ""},
                ]},
            ],
        }
        digest = nd.map_claude_output(data, self.collected, self.cfg)
        nova = next(s for s in digest["sections"] if s["section"] == "dc_nova")["items"]
        self.assertEqual(len(nova), 1)
        self.assertEqual(nova[0]["url"], i66["url"])
        self.assertEqual(nova[0]["headline"], "I-66 lanes open")
        self.assertEqual(nova[0]["jurisdiction"], "Fairfax County")
        io_items = next(s for s in digest["sections"] if s["section"] == "us_industrial_organization")["items"]
        self.assertEqual(io_items[0]["headline"], ftc["title"])   # empty headline falls back to title
        self.assertEqual(digest["top_line"], "A day of tolls and mergers.")
        self.assertEqual([s["section"] for s in digest["sections"]][:2], ["dc_nova", "dc_dc"])

    def test_validate_digest_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            nd.validate_digest({"sections": [{"section": "x", "items": [{"headline": "no url"}]}]})
        with self.assertRaises(ValueError):
            nd.validate_digest([])
        nd.validate_digest({"sections": [{"section": "archive", "items": [
            {"headline": "landmark", "coverage_url": "https://google"}]}]})   # landmarks may lack url


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
    def test_collect_send_round_trip_via_cli_with_landmark_merge(self):
        fx = Fixture()
        try:
            d = Path(fx.dir.name)
            items = d / "items.json"
            rc = nd.main(["collect", "--config", str(fx.config_path), "--out", str(items), "--archive-day", "0"])
            self.assertEqual(rc, 0)
            payload = json.loads(items.read_text())
            self.assertGreater(payload["stats"]["items_kept"], 0)
            self.assertEqual(payload["archive"]["phase"], "landmarks")
            first = next(s for s in payload["sections"] if s["key"] == "dc_nova")["items"][0]
            # A digest a Claude session would write from the collected items (no archive section):
            digest = {"top_line": "Test.", "sections": [{"section": "dc_nova", "items": [
                {"headline": first["title"], "url": first["url"], "source": first["source"],
                 "published": first["published"], "summary": "S.", "why_it_matters": "",
                 "jurisdiction": first["jurisdiction"], "level": first["level"]}]}]}
            dpath = d / "digest.json"
            dpath.write_text(json.dumps(digest))
            out = d / "digest.html"
            rc = nd.main(["send", "--config", str(fx.config_path), "--digest", str(dpath),
                          "--out", str(out), "--dry-run", "--archive-day", "0"])
            self.assertEqual(rc, 0)
            html_out = out.read_text()
            self.assertIn(first["title"], html_out)
            self.assertIn("Northern Virginia", html_out)              # label filled from config
            self.assertIn("The 495 Express Lanes open", html_out)     # landmarks merged by `send`
            md = out.with_suffix(".md").read_text()
            self.assertIn("## From the archive", md)
        finally:
            fx.cleanup()

    def test_run_all_feeds_dead_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "feeds.toml"
            cfg.write_text(CONFIG_TEMPLATE.format(
                transit=Path(td) / "a", general=Path(td) / "b", local=Path(td) / "c", gnews=Path(td) / "d",
                world=Path(td) / "e", broken=Path(td) / "f", missing=Path(td) / "g", landmarks=Path(td) / "h.toml"))
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

    def test_archive_plan_command(self):
        fx = Fixture()
        try:
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = nd.main(["archive-plan", "--config", str(fx.config_path), "--date", "2026-09-16", "--days", "3"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("landmarks", out)
            self.assertIn("Week of January 4, 2016", out)
        finally:
            fx.cleanup()


class RealConfigTests(unittest.TestCase):
    """The shipped feeds.toml and archive.toml must load and be internally consistent."""

    def test_shipped_config_loads(self):
        cfg = nd.load_config(nd.DEFAULT_CONFIG)
        self.assertGreater(len(cfg["feed"]), 40)
        self.assertEqual(list(cfg["regions"]), ["nova", "dc", "maryland", "regional"])
        for feed in cfg["feed"]:
            self.assertTrue(feed["url"].startswith("http"), feed["name"])
        gn = [f for f in cfg["feed"] if "google_news" in f]
        self.assertGreater(len(gn), 5)
        self.assertIn("when%3A2d", gn[0]["url"])
        landmarks = nd.load_landmarks(cfg)
        self.assertGreater(len(landmarks), 40)
        self.assertGreater(sum(1 for l in landmarks if l["tier"] == 1), 15)
        for l in landmarks:
            self.assertIn(l["confidence"], ("high", "medium"), l["title"])
            self.assertTrue(l["query"], l["title"])
        plan = nd.archive_plan(cfg, date(2026, 9, 16), landmarks)
        self.assertEqual(plan["phase"], "landmarks")
        self.assertGreaterEqual(len(plan["landmarks"]), 4)


if __name__ == "__main__":
    unittest.main()
