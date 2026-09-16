#!/usr/bin/env python3
"""
news_digest.py — build and email a daily news digest.

Focus (configured in feeds.toml): the Washington region's traffic and transportation policy
first, with Northern Virginia at state, county, and city level ahead of the District and
Maryland; then United States news on transportation, energy, environment, and industrial
organization; then a short world section; then an archive section that walks through the
region's transportation history: one past year per email starting with last year and
moving back, then one quarter per email, each with curated landmarks plus policy-focused
dated Google News searches, at least ten items when the sources allow).

Sources are RSS / Atom feeds and Google News RSS queries. Ranking and summaries come from
the Claude API when ANTHROPIC_API_KEY is set; otherwise items are ranked by keyword
relevance and shown with the feed's own blurb.

Subcommands
  collect      fetch feeds -> candidate items JSON (what /news-digest hands to Claude)
  send         render a digest JSON -> HTML + text email -> SMTP (or write to --out)
  run          collect + summarize + send, end to end (what GitHub Actions runs)
  check-feeds  per-feed health report (HTTP status, item counts, parse errors)
  archive-plan show which archive chunk today's (or a given day's) digest carries

Environment (only needed for sending / AI summaries)
  SMTP_HOST, SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD
  SMTP_SECURITY  starttls (default) | ssl | none
  DIGEST_TO      comma-separated recipients
  DIGEST_FROM    defaults to SMTP_USERNAME
  ANTHROPIC_API_KEY (optional), DIGEST_MODEL (claude-opus-5), DIGEST_EFFORT (medium)

Exit codes
  0  ok (also: skipped by --only-at-hour, which is a normal outcome in CI)
  2  configuration error (missing SMTP settings, bad digest JSON, bad config)
  3  every feed failed (network blocked or all sources dead)

Requires Python 3.11+ (tomllib, zoneinfo), feedparser, requests. anthropic is optional.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "feeds.toml"
USER_AGENT = "news-digest/1.1 (+https://github.com/; RSS reader for a daily research digest)"
FETCH_TIMEOUT = 20
FETCH_WORKERS = 8
SUMMARY_MAX_CHARS = 600
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "cmpid", "ncid",
}
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
GN_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

GROUP_LABELS = {
    "dc": "Washington region: traffic and transportation policy",
    "us": "United States",
    "world": "World",
    "archive": "From the archive",
}
DEFAULT_LIMITS = {"world": 4, "archive": 14}
PERIOD_KINDS = ("year", "half", "quarter", "month", "week")
DEFAULT_DC_LIMIT = 6
DEFAULT_US_LIMIT = 5


# --------------------------------------------------------------------------- data model

@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    topic: str
    section: str
    published: str          # ISO 8601, UTC
    summary: str
    score: float
    keyword_hits: int = 0
    region: str = ""
    jurisdiction: str = ""
    level: str = ""


@dataclass
class FeedHealth:
    name: str
    url: str
    topic: str
    region: str = "us"
    status: str = "pending"  # ok | http-error | error | parse-error | empty
    http_status: int | None = None
    items_total: int = 0
    items_in_window: int = 0
    items_kept: int = 0
    error: str = ""


@dataclass
class Collected:
    generated_at: str
    window_start: str
    lookback_hours: float
    timezone: str
    sections: list[dict] = field(default_factory=list)   # [{key, label, group, group_label, items}]
    feed_health: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    archive: dict = field(default_factory=dict)          # plan + landmark items for today


# --------------------------------------------------------------------------- config

def google_news_url(query: str) -> str:
    return GN_SEARCH.format(q=quote_plus(query))


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict:
    path = Path(path)
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg["_path"] = path
    s = cfg.setdefault("settings", {})
    s.setdefault("lookback_hours", 26)
    s.setdefault("max_candidates_per_section", 25)
    s.setdefault("timezone", "America/New_York")
    s.setdefault("require_date", True)
    s.setdefault("default_local_region", "regional")
    s.setdefault("google_news_recency", "when:2d")
    cfg["_traffic_regex"] = compile_keywords(s.get("traffic_keywords", []))

    if not cfg.get("topics"):
        raise ValueError(f"{path}: no [topics.*] tables defined")
    if not cfg.get("feed"):
        raise ValueError(f"{path}: no [[feed]] entries defined")
    for key, topic in cfg["topics"].items():
        topic.setdefault("label", key.replace("_", " ").title())
        topic["_regex"] = compile_keywords(topic.get("keywords", []))

    cfg.setdefault("regions", {})
    for key, region in cfg["regions"].items():
        region.setdefault("label", key.replace("_", " ").title())
        region.setdefault("boost", 1.0)
        region["_regex"] = compile_keywords(region.get("keywords", []))
        region["_exclude"] = compile_keywords(region.get("exclude", []))

    cfg.setdefault("jurisdiction", [])
    for j in cfg["jurisdiction"]:
        for required in ("label", "level", "keywords"):
            if required not in j:
                raise ValueError(f"{path}: a [[jurisdiction]] entry is missing '{required}'")
        j["_regex"] = compile_keywords(j["keywords"])

    cfg.setdefault("sections", {})
    cfg.setdefault("groups", {})
    cfg.setdefault("limits", {})
    a = cfg.setdefault("archive", {})
    a.setdefault("enabled", True)
    a.setdefault("anchor", date.today().isoformat())
    a.setdefault("passes", ["year", "quarter"])
    a.setdefault("start_year", date.today().year - 1)
    a.setdefault("end_year", 2016)
    a.setdefault("roots", True)
    a.setdefault("order", "backward")
    a.setdefault("min_items", 10)
    a.setdefault("max_items", 14)
    a.setdefault("queries", [a["query"]] if a.get("query") else [])
    a.setdefault("landmarks_file", "archive.toml")
    for kind in a["passes"]:
        if kind not in PERIOD_KINDS:
            raise ValueError(f"{path}: archive pass {kind!r} must be one of {PERIOD_KINDS}")
    cfg["_policy_regex"] = compile_keywords(a.get("policy_keywords", []))
    cfg["_incident_regex"] = compile_keywords(a.get("incident_keywords", []))

    for feed in cfg["feed"]:
        if "name" not in feed:
            raise ValueError(f"{path}: a [[feed]] entry is missing 'name'")
        if "google_news" in feed and "url" not in feed:
            feed["url"] = google_news_url(f"{feed['google_news']} {s['google_news_recency']}".strip())
        if "url" not in feed:
            raise ValueError(f"{path}: feed {feed['name']!r} needs 'url' or 'google_news'")
        feed.setdefault("topic", "auto")
        feed.setdefault("region", "us")
        feed.setdefault("weight", 1.0)
        if feed["topic"] != "auto" and feed["topic"] not in cfg["topics"]:
            raise ValueError(f"{path}: feed {feed['name']!r} has unknown topic {feed['topic']!r}")
        if feed["region"] not in ("us", "world", "dc"):
            raise ValueError(f"{path}: feed {feed['name']!r} region must be us, world, or dc")
        if feed.get("subregion") and feed["subregion"] not in cfg["regions"]:
            raise ValueError(f"{path}: feed {feed['name']!r} has unknown subregion {feed['subregion']!r}")
    return cfg


def compile_keywords(keywords: list[str]) -> tuple[re.Pattern | None, re.Pattern | None]:
    """Two patterns: case-insensitive words/phrases, and case-sensitive short acronyms
    (e.g. "EV", "DOT", "COP", "DC") so that "cop" or "dot" in ordinary prose do not match."""
    insensitive, sensitive = [], []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        uppercase = sum(1 for c in kw if c.isupper())
        if len(kw) <= 5 and uppercase >= 2:
            sensitive.append(kw)
        else:
            insensitive.append(kw)

    def build(words: list[str], flags: int) -> re.Pattern | None:
        if not words:
            return None
        alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", flags)

    return build(insensitive, re.IGNORECASE), build(sensitive, 0)


def keyword_hits(text: str, patterns: tuple[re.Pattern | None, re.Pattern | None]) -> int:
    hits = 0
    for pat in patterns:
        if pat is not None:
            hits += len(pat.findall(text))
    return hits


def classify(text: str, tables: dict, exclude_key: str | None = None) -> tuple[str, int]:
    """Best key by keyword hits (ties go to the earlier table). ("", 0) when nothing matches.
    With exclude_key, each table's `exclude` pattern subtracts twice its hits."""
    best_key, best_hits = "", 0
    for key, table in tables.items():
        hits = keyword_hits(text, table["_regex"])
        if exclude_key and table.get(exclude_key) is not None:
            hits -= 2 * keyword_hits(text, table[exclude_key])
        if hits > best_hits:
            best_key, best_hits = key, hits
    return best_key, best_hits


LEVEL_RANK = {"town": 0, "city": 0, "county": 1, "state": 2, "regional": 3, "federal": 4}


def detect_jurisdiction(text: str, cfg: dict, region: str) -> tuple[str, str]:
    """Jurisdiction tag for a Washington-region item: the most specific level (town or
    city, then county, then state, then regional) that has any keyword hit. Within a
    level, more hits win; ties go to the earlier config entry. A story is tagged by where
    it happens, so a VDOT project in Fairfax County reads "Fairfax County", not "Virginia"."""
    best, best_key = None, None
    for j in cfg["jurisdiction"]:
        if j.get("region") and j["region"] != region:
            continue
        hits = keyword_hits(text, j["_regex"])
        if not hits:
            continue
        key = (LEVEL_RANK.get(j["level"], 5), -hits)
        if best_key is None or key < best_key:
            best, best_key = j, key
    if best is None:
        return "", ""
    return best["label"], best["level"]


def section_plan(cfg: dict) -> list[dict]:
    """Ordered sections for the email: dc_<region>… us_<topic>… world, archive."""
    plan = []
    for key, region in cfg["regions"].items():
        plan.append({"key": f"dc_{key}", "label": cfg["sections"].get(f"dc_{key}", region["label"]),
                     "group": "dc", "group_label": cfg["groups"].get("dc", GROUP_LABELS["dc"])})
    for key, topic in cfg["topics"].items():
        plan.append({"key": f"us_{key}", "label": cfg["sections"].get(f"us_{key}", topic["label"]),
                     "group": "us", "group_label": cfg["groups"].get("us", GROUP_LABELS["us"])})
    plan.append({"key": "world", "label": cfg["sections"].get("world", "Major stories elsewhere"),
                 "group": "world", "group_label": cfg["groups"].get("world", GROUP_LABELS["world"])})
    if cfg["archive"]["enabled"]:
        plan.append({"key": "archive", "label": cfg["sections"].get("archive", "Region transportation history"),
                     "group": "archive", "group_label": cfg["groups"].get("archive", GROUP_LABELS["archive"])})
    return plan


def cap_for(cfg: dict, key: str) -> int:
    limits = cfg["limits"]
    if key in limits:
        return int(limits[key])
    if key.startswith("dc_"):
        return int(limits.get("dc_default", DEFAULT_DC_LIMIT))
    if key.startswith("us_"):
        return int(limits.get("us_default", DEFAULT_US_LIMIT))
    if key == "archive":
        return int(cfg["archive"].get("max_items", DEFAULT_LIMITS["archive"]))
    return int(DEFAULT_LIMITS.get(key, DEFAULT_US_LIMIT))


# --------------------------------------------------------------------------- text utils

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str | None, limit: int = SUMMARY_MAX_CHARS) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if end < limit // 2:
            end = cut.rfind(" ")
        text = cut[: end + 1].rstrip() + " …"
    return text


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:12]


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def entry_summary(entry) -> str:
    content = entry.get("content")
    if content and isinstance(content, list) and content[0].get("value"):
        return clean_text(content[0]["value"])
    return clean_text(entry.get("summary") or entry.get("description") or "")


def entry_source(entry, default: str) -> str:
    """Google News items carry the real outlet in <source>; other feeds use the feed name."""
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return clean_text(src["title"], 80)
    return default


# --------------------------------------------------------------------------- fetching

def fetch_bytes(url: str, timeout: int = FETCH_TIMEOUT) -> tuple[bytes, int | None]:
    """Return (content, http_status). Local paths and file:// URLs are read from disk
    so the pipeline can be exercised without network access."""
    if url.startswith("file://"):
        return Path(url[7:]).read_bytes(), None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url) and Path(url).exists():
        return Path(url).read_bytes(), None
    import requests  # imported lazily so tests without network never touch it

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.content, resp.status_code


def classify_item(feed_cfg: dict, cfg: dict, text: str) -> dict | None:
    """Decide where an item belongs. Returns None to drop it.

    Rules:
      world feeds      -> section "world" when a topic matches, else drop
      any feed         -> a Washington-region section when region keywords hit AND the item
                          is about transportation or traffic (local feeds default to their
                          subregion when no region keyword resolves)
      otherwise        -> the US section for the matched topic, else drop
    """
    topics = cfg["topics"]
    if feed_cfg["topic"] == "auto":
        topic, hits = classify(text, topics)
    else:
        topic = feed_cfg["topic"]
        hits = keyword_hits(text, topics[topic]["_regex"])
    traffic_hits = keyword_hits(text, cfg["_traffic_regex"])

    if feed_cfg["region"] == "world":
        if not topic:
            return None
        return {"section": "world", "topic": topic, "region": "", "hits": hits, "boost": 1.0}

    region, rhits = classify(text, cfg["regions"], exclude_key="_exclude") if cfg["regions"] else ("", 0)
    if feed_cfg["region"] == "dc" and not region:
        region = feed_cfg.get("subregion") or cfg["settings"]["default_local_region"]
        if region not in cfg["regions"]:
            region = ""
    transport_topic = "transportation" if "transportation" in topics else next(iter(topics))
    if region and (topic == transport_topic or traffic_hits > 0):
        return {"section": f"dc_{region}", "topic": transport_topic, "region": region,
                "hits": hits + traffic_hits + rhits, "boost": float(cfg["regions"][region]["boost"])}
    if topic:
        return {"section": f"us_{topic}", "topic": topic, "region": "", "hits": hits, "boost": 1.0}
    return None


def parse_feed(feed_cfg: dict, cfg: dict, now: datetime, window_start: datetime) -> tuple[list[Item], FeedHealth]:
    """Fetch + parse one feed. Any exception is turned into a health record, so one
    misbehaving source can never abort the whole run."""
    try:
        return _parse_feed(feed_cfg, cfg, now, window_start)
    except Exception as exc:  # noqa: BLE001
        return [], FeedHealth(name=feed_cfg["name"], url=feed_cfg["url"], topic=feed_cfg["topic"],
                              region=feed_cfg.get("region", "us"), status="error",
                              error=f"{type(exc).__name__}: {exc}"[:200])


def _parse_feed(feed_cfg: dict, cfg: dict, now: datetime, window_start: datetime) -> tuple[list[Item], FeedHealth]:
    import feedparser  # lazy: keeps `send` usable in minimal environments

    health = FeedHealth(name=feed_cfg["name"], url=feed_cfg["url"], topic=feed_cfg["topic"],
                        region=feed_cfg.get("region", "us"))
    try:
        content, status = fetch_bytes(feed_cfg["url"])
        health.http_status = status
    except Exception as exc:  # noqa: BLE001 — one dead feed must never abort the run
        health.status = "http-error" if "HTTP" in str(exc) else "error"
        health.error = str(exc)[:200]
        return [], health

    parsed = feedparser.parse(content)
    entries = list(parsed.get("entries") or [])
    health.items_total = len(entries)
    if not entries:
        head = content[:4096].lower()
        if not any(tag in head for tag in (b"<rss", b"<feed", b"<rdf")):
            health.status = "parse-error"
            health.error = "response is not an RSS/Atom feed"
        elif parsed.get("bozo"):
            health.status = "parse-error"
            health.error = str(parsed.get("bozo_exception", "unparseable feed"))[:200]
        else:
            health.status = "empty"
        return [], health

    require_date = bool(cfg["settings"].get("require_date", True))
    weight = float(feed_cfg.get("weight", 1.0))
    items: list[Item] = []
    for entry in entries:
        link = (entry.get("link") or "").strip()
        title = clean_text(entry.get("title") or "", limit=300)
        if not link or not title:
            continue
        source = entry_source(entry, feed_cfg["name"])
        if source != feed_cfg["name"] and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        published = entry_datetime(entry)
        if published is None:
            if require_date:
                continue
            published = now
        if published < window_start or published > now + timedelta(hours=2):
            continue
        health.items_in_window += 1
        summary = entry_summary(entry)
        verdict = classify_item(feed_cfg, cfg, f"{title}. {summary}")
        if verdict is None:
            continue
        age_hours = (now - published).total_seconds() / 3600
        recency = 1.0 if feed_cfg.get("flat_recency") else (1.0 if age_hours <= 12 else 0.85 if age_hours <= 24 else 0.7)
        score = round(weight * verdict["boost"] * (1.0 + 0.35 * min(verdict["hits"], 6)) * recency, 4)
        jurisdiction, level = ("", "")
        if verdict["section"].startswith("dc_"):
            jurisdiction, level = detect_jurisdiction(f"{title}. {summary}", cfg, verdict["region"])
        items.append(Item(
            id=item_id(link), title=title, url=link, source=source, topic=verdict["topic"],
            section=verdict["section"],
            published=published.astimezone(timezone.utc).isoformat(timespec="seconds"),
            summary=summary, score=score, keyword_hits=verdict["hits"], region=verdict["region"],
            jurisdiction=jurisdiction, level=level,
        ))
    health.items_kept = len(items)
    health.status = "ok"
    return items, health


def dedupe(items: list[Item]) -> list[Item]:
    """Collapse the same story seen through several feeds (same URL or same title),
    keeping the highest-scoring copy."""
    best: dict[str, Item] = {}
    title_index: dict[str, str] = {}
    for it in sorted(items, key=lambda i: i.score, reverse=True):
        key = canonical_url(it.url)
        tkey = normalized_title(it.title)
        if key in best:
            continue
        if tkey and tkey in title_index:
            continue
        best[key] = it
        if tkey:
            title_index[tkey] = key
    return list(best.values())


def fetch_all(feeds: list[dict], cfg: dict, now: datetime, window_start: datetime) -> tuple[list[Item], list[FeedHealth]]:
    items: list[Item] = []
    health: list[FeedHealth] = []
    with cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = [pool.submit(parse_feed, f, cfg, now, window_start) for f in feeds]
        for fut in cf.as_completed(futures):
            got, h = fut.result()
            items.extend(got)
            health.append(h)
    return items, health


def collect(cfg: dict, hours: float | None = None, now: datetime | None = None,
            only_sections: list[str] | None = None, archive: bool = True,
            archive_day: int | None = None, archive_period: str | None = None) -> Collected:
    now = now or datetime.now(timezone.utc)
    hours = float(hours or cfg["settings"]["lookback_hours"])
    window_start = now - timedelta(hours=hours)
    tz = ZoneInfo(cfg["settings"]["timezone"])
    plan = section_plan(cfg)
    wanted = {p["key"] for p in plan}
    if only_sections:
        wanted = {k for k in wanted if k in only_sections or k.split("_")[0] in only_sections}

    all_items, health = fetch_all(cfg["feed"], cfg, now, window_start)
    deduped = dedupe(all_items)
    cap_candidates = int(cfg["settings"]["max_candidates_per_section"])

    archive_info: dict = {"phase": "off"}
    archive_items: list[Item] = []
    if archive and cfg["archive"]["enabled"] and "archive" in wanted:
        landmarks = load_landmarks(cfg)
        archive_info = archive_plan(cfg, now.astimezone(tz).date(), landmarks,
                                    day_override=archive_day, period_override=archive_period)
        archive_items, archive_health = archive_collect(cfg, archive_info, now)
        health.extend(archive_health)
        archive_items = dedupe(archive_items)
        archive_info["landmark_items"] = [landmark_to_item(l, cfg) for l in archive_info.pop("landmarks", [])]
        archive_info["min_items"] = int(cfg["archive"].get("min_items", 10))

    health.sort(key=lambda h: (h.status != "ok", h.name.lower()))
    sections_out = []
    for p in plan:
        if p["key"] not in wanted:
            continue
        pool = archive_items if p["key"] == "archive" else [i for i in deduped if i.section == p["key"]]
        chosen = sorted(pool, key=lambda i: i.score, reverse=True)[:cap_candidates]
        sections_out.append({**p, "items": [asdict(i) for i in chosen]})

    stats = {
        "feeds": len(health),
        "feeds_ok": sum(1 for h in health if h.status == "ok"),
        "items_total": sum(h.items_total for h in health),
        "items_in_window": sum(h.items_in_window for h in health),
        "items_kept": len(deduped) + len(archive_items),
        "items_offered": sum(len(s["items"]) for s in sections_out),
    }
    return Collected(
        generated_at=now.isoformat(timespec="seconds"),
        window_start=window_start.isoformat(timespec="seconds"),
        lookback_hours=hours,
        timezone=cfg["settings"]["timezone"],
        sections=sections_out,
        feed_health=[asdict(h) for h in health],
        stats=stats,
        archive=archive_info,
    )


# --------------------------------------------------------------------------- archive

def parse_landmark_date(raw: str) -> tuple[date, str]:
    raw = str(raw).strip()
    if re.fullmatch(r"\d{4}", raw):
        return date(int(raw), 7, 1), "year"
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        y, m = raw.split("-")
        return date(int(y), int(m), 15), "month"
    return date.fromisoformat(raw), "day"


def load_landmarks(cfg: dict) -> list[dict]:
    path = Path(cfg["archive"]["landmarks_file"])
    if not path.is_absolute():
        path = Path(cfg["_path"]).parent / path
    if not path.exists():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out = []
    for raw in data.get("landmark", []):
        for required in ("date", "title", "summary"):
            if required not in raw:
                raise ValueError(f"{path}: a [[landmark]] entry is missing '{required}'")
        d, precision = parse_landmark_date(raw["date"])
        out.append({**raw, "_date": d, "precision": precision, "tier": int(raw.get("tier", 2)),
                    "confidence": raw.get("confidence", "high"), "jurisdiction": raw.get("jurisdiction", ""),
                    "level": raw.get("level", ""), "query": raw.get("query", raw["title"])})
    out.sort(key=lambda l: l["_date"])
    return out


def landmarks_between(landmarks: list[dict], start: date, end: date) -> list[dict]:
    return [l for l in landmarks if start <= l["_date"] < end]


def landmarks_this_week_in_history(landmarks: list[dict], today: date) -> list[dict]:
    lo, hi = today - timedelta(days=3), today + timedelta(days=3)
    out = []
    for l in landmarks:
        if l["precision"] != "day":
            continue
        try:
            same_year = l["_date"].replace(year=today.year)
        except ValueError:  # Feb 29
            continue
        if lo <= same_year <= hi:
            out.append(l)
    return out


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def period_bounds(kind: str, start: date) -> tuple[date, date]:
    if kind == "year":
        return date(start.year, 1, 1), date(start.year + 1, 1, 1)
    if kind == "half":
        first = date(start.year, 1 if start.month <= 6 else 7, 1)
        return first, _add_months(first, 6)
    if kind == "quarter":
        first = date(start.year, 3 * ((start.month - 1) // 3) + 1, 1)
        return first, _add_months(first, 3)
    if kind == "month":
        first = date(start.year, start.month, 1)
        return first, _add_months(first, 1)
    monday = start - timedelta(days=start.weekday())
    return monday, monday + timedelta(days=7)


def period_label(kind: str, start: date) -> str:
    if kind == "year":
        return str(start.year)
    if kind == "half":
        return f"{start.year}, {'first' if start.month == 1 else 'second'} half"
    if kind == "quarter":
        return f"{start.year} Q{(start.month - 1) // 3 + 1}"
    if kind == "month":
        return start.strftime("%B %Y")
    return "Week of " + start.strftime("%B %d, %Y").replace(" 0", " ")


def period_list(kind: str, cfg: dict, today: date) -> list[tuple[date, date, str]]:
    """Complete periods of one kind, from end_year up to start_year (years) or up to the
    last complete period before today (finer kinds), in the configured order."""
    a = cfg["archive"]
    end_year, start_year = int(a["end_year"]), int(a["start_year"])
    periods: list[tuple[date, date]] = []
    if kind == "year":
        periods = [(date(y, 1, 1), date(y + 1, 1, 1)) for y in range(end_year, start_year + 1)]
    else:
        cur, _ = period_bounds(kind, date(end_year, 1, 1))
        while True:
            s_, e_ = period_bounds(kind, cur)
            if e_ > today:
                break
            periods.append((s_, e_))
            cur = e_
    if a.get("order", "backward") == "backward":
        periods.reverse()
    return [(s_, e_, period_label(kind, s_)) for s_, e_ in periods]


def parse_period_override(text: str) -> tuple[str, date]:
    """'2025' -> year; '2025-Q3' / '2025-H1' -> quarter / half; '2025-03' -> month;
    '2025-03-02' -> the week containing that date."""
    t = text.strip().upper()
    if re.fullmatch(r"\d{4}", t):
        return "year", date(int(t), 1, 1)
    m = re.fullmatch(r"(\d{4})-Q([1-4])", t)
    if m:
        return "quarter", date(int(m.group(1)), 3 * (int(m.group(2)) - 1) + 1, 1)
    m = re.fullmatch(r"(\d{4})-H([12])", t)
    if m:
        return "half", date(int(m.group(1)), 1 if m.group(2) == "1" else 7, 1)
    if re.fullmatch(r"\d{4}-\d{2}", t):
        return "month", date.fromisoformat(t + "-01")
    return "week", date.fromisoformat(t)


def archive_chunks(cfg: dict, today: date) -> list[tuple]:
    """The whole archive schedule as an ordered list of chunks: ('period', kind, start,
    end, label) entries for each configured pass, with one ('roots',) chunk after the
    first pass when enabled."""
    a = cfg["archive"]
    chunks: list[tuple] = []
    for i, kind in enumerate(a["passes"]):
        for s_, e_, label in period_list(kind, cfg, today):
            chunks.append(("period", kind, s_, e_, label))
        if i == 0 and a.get("roots"):
            chunks.append(("roots",))
    return chunks


def archive_plan(cfg: dict, today: date, landmarks: list[dict],
                 day_override: int | None = None, period_override: str | None = None) -> dict:
    """Stateless schedule: day d since `anchor` picks chunk d of archive_chunks(). With the
    default passes ["year", "quarter"], day 0 is last year, day 1 the year before, down to
    end_year, then a "policy roots" chunk of earlier landmarks, then one quarter per day
    from the most recent complete quarter back to end_year. Past the last chunk the section
    shows landmarks from this calendar week in earlier years."""
    a = cfg["archive"]
    if not a["enabled"]:
        return {"phase": "off"}
    if period_override:
        kind, start = parse_period_override(period_override)
        s_, e_ = period_bounds(kind, start)
        return _period_plan(kind, s_, e_, period_label(kind, s_), None, None, landmarks, a)
    anchor = date.fromisoformat(str(a["anchor"]))
    d = day_override if day_override is not None else (today - anchor).days
    if d < 0:
        return {"phase": "off", "title": f"Archive starts {anchor.isoformat()}"}
    chunks = archive_chunks(cfg, today)
    if d >= len(chunks):
        return {"phase": "anniversary", "landmarks": landmarks_this_week_in_history(landmarks, today),
                "title": "This week in earlier years",
                "note": "The archive has walked through every configured period. Landmarks that fall in "
                        "this calendar week are shown."}
    chunk = chunks[d]
    if chunk[0] == "roots":
        cutoff = date(int(a["end_year"]), 1, 1)
        return {"phase": "roots", "landmarks": [l for l in landmarks if l["_date"] < cutoff],
                "index": d + 1, "of": len(chunks),
                "title": f"Policy roots before {a['end_year']} ({d + 1} of {len(chunks)})",
                "note": "Curated landmarks from before the year-by-year archive begins. Each links to a "
                        "dated web search for contemporary coverage."}
    _, kind, s_, e_, label = chunk
    return _period_plan(kind, s_, e_, label, d + 1, len(chunks), landmarks, a)


def _period_plan(kind: str, s_: date, e_: date, label: str, index: int | None, total: int | None,
                 landmarks: list[dict], a: dict) -> dict:
    title = f"{label} in review" if kind == "year" else label
    if index and total:
        title += f" ({index} of {total})"
    return {"phase": "period", "kind": kind, "period_start": s_.isoformat(), "period_end": e_.isoformat(),
            "label": label, "index": index, "of": total, "landmarks": landmarks_between(landmarks, s_, e_),
            "title": title,
            "note": f"The most consequential transportation policy developments for the Washington region "
                    f"in {label}: curated landmarks first, then coverage found through dated news searches and "
                    f"ranked for policy relevance, at least {a.get('min_items', 10)} items when the sources allow."}


def coverage_search_url(query: str, d: date, precision: str) -> str:
    if precision == "day":
        lo, hi = d - timedelta(days=21), d + timedelta(days=21)
    elif precision == "month":
        lo, hi = d.replace(day=1) - timedelta(days=10), d.replace(day=28) + timedelta(days=14)
    else:
        lo, hi = date(d.year, 1, 1), date(d.year, 12, 31)
    fmt = lambda x: x.strftime("%m/%d/%Y")
    return f"https://www.google.com/search?q={quote_plus(query)}&tbs=cdr:1,cd_min:{fmt(lo)},cd_max:{fmt(hi)}"


def landmark_date_label(l: dict) -> str:
    d = l["_date"]
    if l["precision"] == "day":
        return d.strftime("%B %d, %Y").replace(" 0", " ")
    if l["precision"] == "month":
        return d.strftime("%B %Y")
    return str(d.year)


def landmark_to_item(l: dict, cfg: dict) -> dict:
    return {
        "kind": "landmark",
        "headline": l["title"],
        "url": l.get("url", ""),
        "coverage_url": coverage_search_url(l["query"], l["_date"], l["precision"]),
        "source": "Curated landmark",
        "published": "",
        "event_date": l["_date"].isoformat(),
        "date_label": landmark_date_label(l),
        "jurisdiction": l.get("jurisdiction", ""),
        "level": l.get("level", ""),
        "summary": l["summary"],
        "why_it_matters": l.get("why_it_matters", ""),
        "confidence": l.get("confidence", "high"),
        "tier": l.get("tier", 2),
    }


def archive_collect(cfg: dict, plan: dict, now: datetime) -> tuple[list[Item], list[FeedHealth]]:
    """Fetch the period's coverage through each configured Google News query with date
    operators, keep transport items about the region, score them for policy relevance
    (policy words up, incident-only stories out), and mark them as the archive section."""
    a = cfg["archive"]
    if plan.get("phase") != "period" or not a.get("queries"):
        return [], []
    s_ = date.fromisoformat(plan["period_start"])
    e_ = date.fromisoformat(plan["period_end"])
    start_dt = datetime(s_.year, s_.month, s_.day, tzinfo=timezone.utc)
    end_dt = datetime(e_.year, e_.month, e_.day, tzinfo=timezone.utc)
    subregion = cfg["settings"]["default_local_region"]
    items: list[Item] = []
    health: list[FeedHealth] = []
    for n, query in enumerate(a["queries"], 1):
        feed_cfg = {"name": f"Google News archive {n} ({plan['label']})",
                    "url": google_news_url(f"{query} after:{s_.isoformat()} before:{e_.isoformat()}"),
                    "topic": "auto", "region": "dc", "weight": 1.0, "flat_recency": True}
        if subregion in cfg["regions"]:
            feed_cfg["subregion"] = subregion
        got, h = parse_feed(feed_cfg, cfg, now=end_dt, window_start=start_dt)
        kept = []
        for it in got:
            if not (it.section.startswith("dc_") or it.topic == "transportation"):
                continue
            text = f"{it.title}. {it.summary}"
            policy = keyword_hits(text, cfg["_policy_regex"])
            incident = keyword_hits(text, cfg["_incident_regex"])
            if incident and not policy:
                continue
            it.score = round(it.score * (1 + 0.5 * min(policy, 6)) / (1 + 0.5 * incident), 4)
            it.section = "archive"
            kept.append(it)
        h.items_kept = len(kept)
        items.extend(kept)
        health.append(h)
    return items, health


# --------------------------------------------------------------------------- summarizing

SYSTEM_PROMPT = """You compile a daily news digest for an academic economist whose research spans transportation, energy, environment, and industrial organization / antitrust, and who uses the digest to gather research ideas.

The digest has four groups, in this order of priority:
1. Washington region traffic and transportation policy, split into Northern Virginia (the top priority: state, county, and city or town actions, road and transit conditions, tolling, transit funding), the District of Columbia, Maryland, and region-wide bodies such as WMATA. Prefer concrete policy actions, project milestones, funding decisions, enforcement changes, data releases, and notable local reporting over routine incident reports; a single crash is rarely worth including unless it changed policy or closed a corridor for a long time.
2. United States news on each topic: policy and regulatory actions, enforcement cases and court decisions, market and price moves, major corporate decisions, data releases, and notable research.
3. Major stories elsewhere in the world: a few items only, chosen for research relevance rather than completeness.
4. An archive section when present: the most consequential transportation policy developments for the Washington region in one past period (a year or a quarter). Pick at least the section's min_items when the candidates allow, favoring policy decisions, funding and tolling changes, project openings and cancellations, governance and enforcement changes, and major studies over crashes, closures, and routine service notices. Order by importance, not date. Write each as a short retrospective in the past tense, naming the month and year.

You receive candidate items grouped by section. Each item has an id, title, source, publication time, URL, and the feed's own blurb. Select and summarize. Skip marketing, listicles, opinion with no news, and near-duplicates of a story already chosen in any section. Balance sources; do not let one outlet dominate a section.

Ground every summary strictly in the provided title and blurb. Do not add facts, numbers, or names that are not in the input. If a blurb is thin, keep the summary short rather than speculating. Never invent items: every id you return must come from the input.

Write plainly. No hype, no em-dashes."""


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "top_line": {
            "type": "string",
            "description": "Three to five sentences on the day's most consequential developments, leading with the Washington region when it has substantive news.",
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "section key copied from the input"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "id copied verbatim from the input"},
                                "headline": {"type": "string", "description": "Concise headline; may lightly rephrase the title"},
                                "summary": {"type": "string", "description": "Two or three sentences grounded in the blurb"},
                                "why_it_matters": {"type": "string", "description": "One sentence on relevance to research or policy; empty string if nothing non-obvious to say"},
                            },
                            "required": ["id", "headline", "summary", "why_it_matters"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["section", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["top_line", "sections"],
    "additionalProperties": False,
}


def _candidates_for_prompt(collected: Collected) -> dict:
    out = {}
    for s in collected.sections:
        out[s["key"]] = {
            "label": f"{s['group_label']} / {s['label']}",
            "max_items": None,  # filled by caller
            **({"min_items": int(collected.archive.get("min_items", 0))} if s["key"] == "archive" and collected.archive.get("min_items") else {}),
            "items": [
                {"id": i["id"], "title": i["title"], "source": i["source"], "published": i["published"],
                 "url": i["url"], "blurb": i["summary"],
                 **({"jurisdiction": i["jurisdiction"]} if i.get("jurisdiction") else {})}
                for i in s["items"]
            ],
        }
    return out


def map_claude_output(data: dict, collected: Collected, cfg: dict) -> dict:
    """Turn the model's picks into full digest sections, resolving each id against the
    collected items so URLs, sources, and timestamps come from the feed, never the model."""
    by_id = {i["id"]: i for s in collected.sections for i in s["items"]}
    picked = {s.get("section") or s.get("topic"): s.get("items", []) for s in data.get("sections", [])}
    seen: set[str] = set()
    sections = []
    for s in collected.sections:
        key = s["key"]
        items_out = []
        for pick in picked.get(key, []):
            src = by_id.get(pick.get("id"))
            if src is None or src["id"] in seen:
                continue
            seen.add(src["id"])
            items_out.append({
                "headline": (pick.get("headline") or src["title"]).strip(),
                "url": src["url"],
                "source": src["source"],
                "published": src["published"],
                "summary": (pick.get("summary") or src["summary"]).strip(),
                "why_it_matters": (pick.get("why_it_matters") or "").strip(),
                "jurisdiction": src.get("jurisdiction", ""),
                "level": src.get("level", ""),
            })
            if len(items_out) >= cap_for(cfg, key):
                break
        sections.append({"section": key, "label": s["label"], "group": s["group"],
                         "group_label": s["group_label"], "items": items_out})
    return {"top_line": (data.get("top_line") or "").strip(), "sections": sections}


def summarize_with_claude(collected: Collected, cfg: dict) -> dict | None:
    """Rank + summarize with the Claude API. Returns None (caller falls back) on any
    API problem, so the email still goes out."""
    try:
        import anthropic
    except ImportError:
        log("anthropic SDK not installed; using keyword ranking")
        return None
    model = os.environ.get("DIGEST_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("DIGEST_EFFORT", DEFAULT_EFFORT)
    payload = _candidates_for_prompt(collected)
    for key in payload:
        payload[key]["max_items"] = cap_for(cfg, key)
    if not any(v["items"] for v in payload.values()):
        return None
    user_text = (
        f"Date: {collected.generated_at}. For each section pick up to its max_items, ordered by importance; "
        "return every section key even when you pick nothing for it.\n\n"
        "Candidates (JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    output_config: dict = {"format": {"type": "json_schema", "schema": DIGEST_SCHEMA}}
    if not model.startswith("claude-haiku"):
        output_config["effort"] = effort
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config=output_config,
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.AuthenticationError:
        log("Claude API: invalid ANTHROPIC_API_KEY; using keyword ranking")
        return None
    except anthropic.RateLimitError:
        log("Claude API: rate limited; using keyword ranking")
        return None
    except anthropic.APIStatusError as exc:
        log(f"Claude API: HTTP {exc.status_code} {getattr(exc, 'message', '')}; using keyword ranking")
        return None
    except anthropic.APIConnectionError as exc:
        log(f"Claude API: connection error ({exc}); using keyword ranking")
        return None
    if response.stop_reason == "refusal":
        log("Claude API: request declined by safety classifier; using keyword ranking")
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log("Claude API: response was not valid JSON; using keyword ranking")
        return None
    digest = map_claude_output(data, collected, cfg)
    digest["generated_by"] = f"claude-api:{model}"
    usage = getattr(response, "usage", None)
    if usage is not None:
        log(f"Claude API: {model} in={usage.input_tokens} out={usage.output_tokens}")
    return digest


def summarize_fallback(collected: Collected, cfg: dict) -> dict:
    sections = []
    for s in collected.sections:
        items = []
        for i in s["items"][: cap_for(cfg, s["key"])]:
            items.append({
                "headline": i["title"], "url": i["url"], "source": i["source"],
                "published": i["published"], "summary": clean_text(i["summary"], 320),
                "why_it_matters": "", "jurisdiction": i.get("jurisdiction", ""), "level": i.get("level", ""),
            })
        sections.append({"section": s["key"], "label": s["label"], "group": s["group"],
                         "group_label": s["group_label"], "items": items})
    n = sum(len(s["items"]) for s in sections)
    top_line = (
        f"{n} items selected by keyword relevance from {collected.stats.get('feeds_ok', 0)} sources "
        f"over the last {collected.lookback_hours:g} hours. Summaries are the feeds' own blurbs. "
        "Set ANTHROPIC_API_KEY to get ranked, written summaries."
    )
    return {"top_line": top_line, "sections": sections, "generated_by": "keyword-fallback"}


def merge_archive(digest: dict, archive: dict, cfg: dict, candidates: list[dict] | None = None) -> None:
    """Attach the archive plan's landmarks and note to the digest's archive section,
    creating the section if the summarizer (or a Claude session) left it out. Landmarks
    come first; searched items are topped up from `candidates` to reach min_items and the
    whole section is capped at max_items (landmarks are never dropped)."""
    if not archive or archive.get("phase") in (None, "off"):
        return
    section = next((s for s in digest["sections"] if (s.get("section") or s.get("topic")) == "archive"), None)
    if section is None:
        section = {"section": "archive", "label": cfg["sections"].get("archive", "Region transportation history"),
                   "group": "archive", "group_label": cfg["groups"].get("archive", GROUP_LABELS["archive"]),
                   "items": []}
        digest["sections"].append(section)
    section.setdefault("note", archive.get("note", ""))
    section["title"] = archive.get("title", "")
    existing = {i.get("headline") for i in section["items"] if i.get("kind") == "landmark"}
    landmarks = [l for l in archive.get("landmark_items", []) if l["headline"] not in existing]
    searched = [i for i in section["items"] if i.get("kind") != "landmark"]
    a = cfg["archive"]
    min_items, max_items = int(a.get("min_items", 10)), cap_for(cfg, "archive")
    if candidates:
        chosen = {i.get("url") for i in searched}
        for cand in sorted(candidates, key=lambda c: c.get("score", 0), reverse=True):
            if len(landmarks) + len(searched) >= min_items:
                break
            if cand["url"] in chosen:
                continue
            chosen.add(cand["url"])
            searched.append({"headline": cand["title"], "url": cand["url"], "source": cand["source"],
                             "published": cand["published"], "summary": clean_text(cand["summary"], 320),
                             "why_it_matters": "", "jurisdiction": cand.get("jurisdiction", ""),
                             "level": cand.get("level", "")})
    section["items"] = landmarks + searched[: max(0, max_items - len(landmarks))]


def build_digest(collected: Collected, cfg: dict, use_ai: bool = True) -> dict:
    digest = None
    if use_ai and os.environ.get("ANTHROPIC_API_KEY"):
        digest = summarize_with_claude(collected, cfg)
    if digest is None:
        digest = summarize_fallback(collected, cfg)
    archive_candidates = next((s["items"] for s in collected.sections if s["key"] == "archive"), [])
    merge_archive(digest, collected.archive, cfg, candidates=archive_candidates)
    tz = ZoneInfo(cfg["settings"]["timezone"])
    digest["date"] = datetime.fromisoformat(collected.generated_at).astimezone(tz).strftime("%Y-%m-%d")
    digest["feed_health"] = collected.feed_health
    digest["stats"] = collected.stats
    digest["lookback_hours"] = collected.lookback_hours
    return digest


# --------------------------------------------------------------------------- rendering

def validate_digest(digest: dict) -> None:
    if not isinstance(digest, dict) or "sections" not in digest:
        raise ValueError("digest JSON must be an object with a 'sections' list")
    for s in digest["sections"]:
        if not (s.get("section") or s.get("topic")):
            raise ValueError(f"section is missing 'section': {s}")
        if "items" not in s:
            raise ValueError(f"section is missing 'items': {s}")
        for i in s["items"]:
            if not i.get("headline"):
                raise ValueError(f"item is missing 'headline': {i}")
            if not (i.get("url") or i.get("coverage_url")):
                raise ValueError(f"item is missing 'url': {i}")


def normalize_sections(digest: dict, cfg: dict) -> None:
    """Fill label/group for sections written by hand (e.g. by a Claude session) and put
    them in the configured order."""
    plan = {p["key"]: p for p in section_plan(cfg)}
    for s in digest["sections"]:
        key = s.get("section") or s.get("topic")
        s["section"] = key
        meta = plan.get(key, {"label": key, "group": "us", "group_label": GROUP_LABELS["us"]})
        s.setdefault("label", meta["label"])
        s.setdefault("group", meta["group"])
        s.setdefault("group_label", meta["group_label"])
    order = list(plan)
    digest["sections"].sort(key=lambda s: order.index(s["section"]) if s["section"] in order else len(order))


def fmt_time(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    return local.strftime("%b %d, %I:%M %p").replace(" 0", " ") + " " + (local.tzname() or "")


def digest_date_label(digest: dict, tz: ZoneInfo) -> str:
    raw = digest.get("date")
    try:
        d = datetime.strptime(raw, "%Y-%m-%d") if raw else datetime.now(tz)
    except ValueError:
        d = datetime.now(tz)
    return d.strftime("%A, %B %d, %Y").replace(" 0", " ")


def subject_line(digest: dict, tz: ZoneInfo) -> str:
    dc_n = sum(len(s["items"]) for s in digest["sections"] if s.get("group") == "dc")
    us_n = sum(len(s["items"]) for s in digest["sections"] if s.get("group") == "us")
    archive = next((s for s in digest["sections"] if s.get("section") == "archive"), None)
    parts = []
    if dc_n:
        parts.append(f"{dc_n} DC-region")
    if us_n:
        parts.append(f"{us_n} US")
    tail = ", ".join(parts) or "no new items"
    if archive and archive.get("title"):
        tail += f"; archive: {archive['title'].split(' (')[0]}"
    return f"News digest {digest_date_label(digest, tz)}: {tail}"


def item_meta(i: dict, tz: ZoneInfo) -> list[str]:
    bits = []
    if i.get("kind") == "landmark":
        bits.append("Landmark")
        if i.get("date_label"):
            bits.append(i["date_label"])
    else:
        if i.get("source"):
            bits.append(i["source"])
        if i.get("published"):
            bits.append(fmt_time(i["published"], tz))
    if i.get("jurisdiction"):
        lvl = f" ({i['level']})" if i.get("level") else ""
        bits.append(f"{i['jurisdiction']}{lvl}")
    if i.get("kind") == "landmark" and i.get("confidence") == "medium":
        bits.append("details to verify")
    return bits


def render_html(digest: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["settings"]["timezone"])
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{e(subject_line(digest, tz))}</title></head>",
        "<body style='margin:0;padding:0;background:#f4f4f2;font-family:Georgia,\"Times New Roman\",serif;color:#1f1f1f;'>",
        "<div style='max-width:680px;margin:0 auto;padding:24px 16px;background:#ffffff;'>",
        "<h1 style='font-size:22px;margin:0 0 4px 0;'>Daily news digest</h1>",
        f"<div style='color:#666;font-size:13px;margin-bottom:18px;'>{e(digest_date_label(digest, tz))}"
        " &middot; Washington region traffic and policy &middot; US transportation, energy, environment, industrial organization</div>",
    ]
    if digest.get("top_line"):
        parts.append(
            "<div style='background:#f7f3e8;border-left:4px solid #b08a2e;padding:12px 14px;margin:0 0 22px 0;font-size:15px;line-height:1.5;'>"
            f"{e(digest['top_line'])}</div>"
        )
    current_group = None
    for s in digest["sections"]:
        if s.get("group") != current_group:
            current_group = s.get("group")
            parts.append(f"<h2 style='font-size:18px;margin:28px 0 6px 0;color:#1a3c6e;'>{e(s.get('group_label', ''))}</h2>")
        parts.append(f"<h3 style='font-size:15px;border-bottom:1px solid #ddd;padding-bottom:4px;margin:16px 0 10px 0;'>{e(s.get('label') or s['section'])}"
                     + (f" <span style='font-weight:normal;color:#666;'>&middot; {e(s['title'])}</span>" if s.get("title") else "")
                     + "</h3>")
        if s.get("note"):
            parts.append(f"<div style='color:#666;font-size:12px;margin:0 0 10px 0;'>{e(s['note'])}</div>")
        if not s.get("items"):
            parts.append("<p style='color:#777;font-size:14px;'>No items in the window.</p>")
            continue
        for i in s["items"]:
            meta = " &middot; ".join(e(b) for b in item_meta(i, tz))
            link = i.get("url") or i.get("coverage_url")
            parts.append("<div style='margin:0 0 16px 0;'>")
            parts.append(f"<a href='{e(link, quote=True)}' style='font-size:15px;font-weight:bold;color:#1a3c6e;text-decoration:none;'>{e(i['headline'])}</a>")
            if meta:
                parts.append(f"<div style='color:#777;font-size:12px;margin:2px 0 4px 0;'>{meta}</div>")
            if i.get("summary"):
                parts.append(f"<div style='font-size:14px;line-height:1.5;'>{e(i['summary'])}</div>")
            if i.get("why_it_matters"):
                parts.append(f"<div style='font-size:13px;line-height:1.45;color:#444;font-style:italic;margin-top:3px;'>Why it matters: {e(i['why_it_matters'])}</div>")
            if i.get("kind") == "landmark" and i.get("coverage_url"):
                parts.append(f"<div style='font-size:12px;margin-top:3px;'><a href='{e(i['coverage_url'], quote=True)}' style='color:#1a3c6e;'>Search contemporary coverage</a></div>")
            parts.append("</div>")
    bad = [h for h in digest.get("feed_health", []) if h.get("status") != "ok"]
    stats = digest.get("stats", {})
    parts.append("<div style='margin-top:30px;padding-top:10px;border-top:1px solid #ddd;color:#888;font-size:12px;line-height:1.5;'>")
    if stats:
        parts.append(
            f"Sources: {stats.get('feeds_ok', 0)} of {stats.get('feeds', 0)} feeds returned items; "
            f"{stats.get('items_in_window', 0)} items in the last {digest.get('lookback_hours', '?')} hours, "
            f"{stats.get('items_kept', 0)} after topic and region filtering and de-duplication.<br>"
        )
    if bad:
        parts.append("Feeds with no items this run: " + ", ".join(
            f"{e(h['name'])} ({e(h['status'])}{': ' + e(h['error'][:60]) if h.get('error') else ''})" for h in bad
        ) + ".<br>")
    parts.append(f"Generated by /news-digest ({e(str(digest.get('generated_by', 'unknown')))}). "
                 "Sources, regions, keywords: <code>scripts/news_digest/feeds.toml</code>; landmarks: <code>archive.toml</code>.")
    parts.append("</div></div></body></html>")
    return "\n".join(parts)


def _text_items(s: dict, tz: ZoneInfo, md: bool) -> list[str]:
    lines = []
    for i in s["items"]:
        meta = " · ".join(item_meta(i, tz))
        link = i.get("url") or i.get("coverage_url")
        if md:
            lines.append(f"- **[{i['headline']}]({link})**" + (f" ({meta})" if meta else ""))
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
            if i.get("why_it_matters"):
                lines.append(f"  _Why it matters: {i['why_it_matters']}_")
            if i.get("kind") == "landmark" and i.get("coverage_url") and i.get("url"):
                lines.append(f"  [Search contemporary coverage]({i['coverage_url']})")
        else:
            lines.append(f"* {i['headline']}")
            if meta:
                lines.append(f"  {meta}")
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
            if i.get("why_it_matters"):
                lines.append(f"  Why it matters: {i['why_it_matters']}")
            lines.append(f"  {link}")
            lines.append("")
    return lines


def render_text(digest: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["settings"]["timezone"])
    lines = [f"DAILY NEWS DIGEST — {digest_date_label(digest, tz)}", ""]
    if digest.get("top_line"):
        lines += [digest["top_line"], ""]
    current_group = None
    for s in digest["sections"]:
        if s.get("group") != current_group:
            current_group = s.get("group")
            g = s.get("group_label", "")
            lines += ["=" * len(g), g.upper(), "=" * len(g), ""]
        label = s.get("label") or s["section"]
        if s.get("title"):
            label += f" · {s['title']}"
        lines += [label, "-" * len(label)]
        if s.get("note"):
            lines += [s["note"], ""]
        if not s.get("items"):
            lines += ["(no items in the window)", ""]
            continue
        lines += _text_items(s, tz, md=False)
        lines.append("")
    bad = [h for h in digest.get("feed_health", []) if h.get("status") != "ok"]
    if bad:
        lines.append("Feeds with no items this run: " + ", ".join(f"{h['name']} ({h['status']})" for h in bad))
    lines.append(f"Generated by /news-digest ({digest.get('generated_by', 'unknown')}).")
    return "\n".join(lines)


def render_markdown(digest: dict, cfg: dict) -> str:
    """Markdown flavour, for saving under quality_reports/ or pasting into a chat."""
    tz = ZoneInfo(cfg["settings"]["timezone"])
    lines = [f"# Daily news digest — {digest_date_label(digest, tz)}", ""]
    if digest.get("top_line"):
        lines += [f"> {digest['top_line']}", ""]
    current_group = None
    for s in digest["sections"]:
        if s.get("group") != current_group:
            current_group = s.get("group")
            lines += [f"## {s.get('group_label', '')}", ""]
        label = s.get("label") or s["section"]
        if s.get("title"):
            label += f" · {s['title']}"
        lines += [f"### {label}", ""]
        if s.get("note"):
            lines += [f"_{s['note']}_", ""]
        if not s.get("items"):
            lines += ["_No items in the window._", ""]
            continue
        lines += _text_items(s, tz, md=True)
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- email

def smtp_settings_from_env(env: dict | None = None) -> dict:
    env = env if env is not None else os.environ
    missing = [k for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "DIGEST_TO") if not env.get(k)]
    if missing:
        raise ValueError("missing email settings: " + ", ".join(missing))
    port = int(env.get("SMTP_PORT") or 587)
    security = (env.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    if security not in ("starttls", "ssl", "none"):
        raise ValueError("SMTP_SECURITY must be starttls, ssl, or none")
    recipients = [r.strip() for r in env["DIGEST_TO"].split(",") if r.strip()]
    return {
        "host": env["SMTP_HOST"], "port": port, "username": env["SMTP_USERNAME"],
        "password": env["SMTP_PASSWORD"], "security": security,
        "sender": env.get("DIGEST_FROM") or env["SMTP_USERNAME"], "recipients": recipients,
    }


def build_message(subject: str, text: str, html_body: str, sender: str, recipients: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="news-digest.local")
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(msg: EmailMessage, settings: dict, smtp_factory=None) -> None:
    """Deliver via SMTP. `smtp_factory` lets tests inject a fake client."""
    if settings["security"] == "ssl":
        factory = smtp_factory or smtplib.SMTP_SSL
        client = factory(settings["host"], settings["port"], timeout=30)
    else:
        factory = smtp_factory or smtplib.SMTP
        client = factory(settings["host"], settings["port"], timeout=30)
    with client as smtp:
        smtp.ehlo()
        if settings["security"] == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if settings["username"]:
            smtp.login(settings["username"], settings["password"])
        smtp.send_message(msg, from_addr=settings["sender"], to_addrs=settings["recipients"])


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def should_run_now(hour: int, tz_name: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(tz_name)).hour == hour


def write_outputs(out: Path, html_body: str, text: str, digest: dict, cfg: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_body, encoding="utf-8")
    out.with_suffix(".txt").write_text(text, encoding="utf-8")
    out.with_suffix(".md").write_text(render_markdown(digest, cfg), encoding="utf-8")
    log(f"wrote {out}, {out.with_suffix('.txt')}, {out.with_suffix('.md')}")


def deliver(digest: dict, cfg: dict, out: str | None, dry_run: bool) -> int:
    validate_digest(digest)
    normalize_sections(digest, cfg)
    tz = ZoneInfo(cfg["settings"]["timezone"])
    html_body = render_html(digest, cfg)
    text = render_text(digest, cfg)
    subject = subject_line(digest, tz)
    if out:
        write_outputs(Path(out), html_body, text, digest, cfg)
    if dry_run:
        log(f"dry run: not sending. Subject would be: {subject}")
        return 0
    try:
        settings = smtp_settings_from_env()
    except ValueError as exc:
        log(f"cannot send: {exc}. Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, DIGEST_TO "
            "(and optionally SMTP_PORT, SMTP_SECURITY, DIGEST_FROM), or pass --dry-run.")
        return 2
    msg = build_message(subject, text, html_body, settings["sender"], settings["recipients"])
    send_email(msg, settings)
    log(f"sent '{subject}' to {', '.join(settings['recipients'])} via {settings['host']}:{settings['port']}")
    return 0


def log_stats(collected: Collected) -> None:
    s = collected.stats
    n_cand = len(next((x["items"] for x in collected.sections if x["key"] == "archive"), []))
    n_land = len(collected.archive.get("landmark_items", []))
    log(f"feeds ok {s['feeds_ok']}/{s['feeds']}; items in window {s['items_in_window']}; "
        f"kept {s['items_kept']}; offered {s['items_offered']}; archive: {collected.archive.get('title', 'off')} "
        f"({n_land} landmarks, {n_cand} searched candidates)")


# --------------------------------------------------------------------------- CLI

def _collect_from_args(cfg: dict, args) -> Collected:
    return collect(cfg, hours=args.hours, only_sections=args.sections, archive=not args.no_archive,
                   archive_day=args.archive_day, archive_period=args.archive_period)


def cmd_collect(args) -> int:
    cfg = load_config(args.config)
    collected = _collect_from_args(cfg, args)
    payload = asdict(collected)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"wrote {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    log_stats(collected)
    return 3 if collected.stats["feeds_ok"] == 0 else 0


def cmd_send(args) -> int:
    cfg = load_config(args.config)
    try:
        digest = json.loads(Path(args.digest).read_text(encoding="utf-8"))
        validate_digest(digest)
    except (OSError, ValueError) as exc:
        log(f"bad digest JSON: {exc}")
        return 2
    normalize_sections(digest, cfg)
    if not args.no_archive and cfg["archive"]["enabled"]:
        tz = ZoneInfo(cfg["settings"]["timezone"])
        landmarks = load_landmarks(cfg)
        plan = archive_plan(cfg, datetime.now(tz).date(), landmarks,
                            day_override=args.archive_day, period_override=args.archive_period)
        plan["landmark_items"] = [landmark_to_item(l, cfg) for l in plan.pop("landmarks", [])]
        merge_archive(digest, plan, cfg)
    digest.setdefault("generated_by", "claude-in-session")
    digest.setdefault("date", datetime.now(ZoneInfo(cfg["settings"]["timezone"])).strftime("%Y-%m-%d"))
    return deliver(digest, cfg, args.out, args.dry_run)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.only_at_hour is not None and not should_run_now(args.only_at_hour, cfg["settings"]["timezone"]):
        local = datetime.now(ZoneInfo(cfg["settings"]["timezone"])).strftime("%H:%M %Z")
        log(f"skipping: local time is {local}, not the {args.only_at_hour}:00 hour")
        return 0
    collected = _collect_from_args(cfg, args)
    log_stats(collected)
    if collected.stats["feeds_ok"] == 0:
        log("every feed failed; nothing to send (network blocked, or all sources dead)")
        for h in collected.feed_health[:10]:
            log(f"  {h['name']}: {h['status']} {h['error']}")
        return 3
    digest = build_digest(collected, cfg, use_ai=not args.no_ai)
    if args.save_json:
        Path(args.save_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_json).write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"wrote {args.save_json}")
    return deliver(digest, cfg, args.out, args.dry_run)


def cmd_check_feeds(args) -> int:
    cfg = load_config(args.config)
    collected = collect(cfg, hours=args.hours, archive=False)
    width = max(len(h["name"]) for h in collected.feed_health) + 2
    print(f"{'feed':<{width}} {'region':<7} {'status':<12} {'http':>5} {'total':>6} {'window':>7} {'kept':>5}  error")
    for h in collected.feed_health:
        print(f"{h['name']:<{width}} {h['region']:<7} {h['status']:<12} {str(h['http_status'] or ''):>5} "
              f"{h['items_total']:>6} {h['items_in_window']:>7} {h['items_kept']:>5}  {h['error'][:70]}")
    s = collected.stats
    print(f"\n{s['feeds_ok']}/{s['feeds']} feeds ok; {s['items_in_window']} items in window; {s['items_kept']} kept")
    return 3 if s["feeds_ok"] == 0 else 0


def cmd_archive_plan(args) -> int:
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["settings"]["timezone"])
    landmarks = load_landmarks(cfg)
    today = date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    for offset in range(args.days):
        plan = archive_plan(cfg, today + timedelta(days=offset), landmarks,
                            day_override=args.archive_day, period_override=args.archive_period)
        marks = plan.get("landmarks", [])
        print(f"{(today + timedelta(days=offset)).isoformat()}  {plan.get('phase'):<12} {plan.get('title', '')}"
              + (f"  [{len(marks)} landmark(s)]" if marks else ""))
        if args.verbose:
            for l in marks:
                print(f"    {landmark_date_label(l):<20} {l['title']}")
    chunks = archive_chunks(cfg, today)
    print(f"\n{len(landmarks)} landmarks loaded ({sum(1 for l in landmarks if l['tier'] == 1)} tier 1); "
          f"{len(chunks)} scheduled chunks before anniversaries")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", default=str(DEFAULT_CONFIG), help="feeds.toml path")

    def archive_opts(sp):
        sp.add_argument("--no-archive", action="store_true", help="omit the archive section")
        sp.add_argument("--archive-day", type=int, metavar="N", help="pretend today is day N of the archive schedule")
        sp.add_argument("--archive-period", metavar="PERIOD",
                        help="force the archive to a period: 2025, 2025-Q3, 2025-H1, 2025-03, or 2025-03-02 (that week)")

    def collect_opts(sp):
        sp.add_argument("--hours", type=float, help="lookback window (default from config)")
        sp.add_argument("--sections", type=lambda s: [t.strip() for t in s.split(",") if t.strip()],
                        help="comma-separated section keys or groups (dc, us, world, archive, us_energy, …)")

    sp = sub.add_parser("collect", help="fetch feeds and write candidate items JSON")
    common(sp); collect_opts(sp); archive_opts(sp)
    sp.add_argument("--out", help="write JSON here instead of stdout")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("send", help="render a digest JSON and email it (or write with --out)")
    common(sp); archive_opts(sp)
    sp.add_argument("--digest", required=True, help="digest JSON produced by Claude or by `run --save-json`")
    sp.add_argument("--out", help="also write .html/.txt/.md files here (path to the .html)")
    sp.add_argument("--dry-run", action="store_true", help="render only; never send")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("run", help="collect + summarize + send")
    common(sp); collect_opts(sp); archive_opts(sp)
    sp.add_argument("--no-ai", action="store_true", help="skip the Claude API even if a key is set")
    sp.add_argument("--out", help="write .html/.txt/.md files here (path to the .html)")
    sp.add_argument("--save-json", help="write the digest JSON here")
    sp.add_argument("--dry-run", action="store_true", help="render only; never send")
    sp.add_argument("--only-at-hour", type=int, metavar="H",
                    help="exit 0 without doing anything unless the local hour (config timezone) is H")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("check-feeds", help="fetch every feed and print a health table")
    common(sp)
    sp.add_argument("--hours", type=float)
    sp.set_defaults(func=cmd_check_feeds)

    sp = sub.add_parser("archive-plan", help="show the archive chunk for today or a range of days")
    common(sp); archive_opts(sp)
    sp.add_argument("--date", help="start date (YYYY-MM-DD), default today")
    sp.add_argument("--days", type=int, default=1, help="how many consecutive days to show")
    sp.add_argument("--verbose", action="store_true", help="list the landmarks in each chunk")
    sp.set_defaults(func=cmd_archive_plan)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
