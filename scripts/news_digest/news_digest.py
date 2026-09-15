#!/usr/bin/env python3
"""
news_digest.py — build and email a daily news digest.

Topics (configured in feeds.toml): transportation, energy, environment, and industrial
organization / antitrust. Sources are RSS / Atom feeds. Ranking and summaries come from
the Claude API when ANTHROPIC_API_KEY is set; otherwise items are ranked by keyword
relevance and shown with the feed's own blurb.

Subcommands
  collect      fetch feeds -> candidate items JSON (what /news-digest hands to Claude)
  send         render a digest JSON -> HTML + text email -> SMTP (or write to --out)
  run          collect + summarize + send, end to end (what GitHub Actions runs)
  check-feeds  per-feed health report (HTTP status, item counts, parse errors)

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
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "feeds.toml"
USER_AGENT = "news-digest/1.0 (+https://github.com/; RSS reader for a daily research digest)"
FETCH_TIMEOUT = 20
FETCH_WORKERS = 8
SUMMARY_MAX_CHARS = 600
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "cmpid", "ncid",
}
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"


# --------------------------------------------------------------------------- data model

@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    topic: str
    published: str          # ISO 8601, UTC
    summary: str
    score: float
    keyword_hits: int = 0


@dataclass
class FeedHealth:
    name: str
    url: str
    topic: str
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
    topics: list[dict] = field(default_factory=list)      # [{key, label, items:[Item dict]}]
    feed_health: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- config

def load_config(path: Path | str = DEFAULT_CONFIG) -> dict:
    path = Path(path)
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("settings", {})
    s = cfg["settings"]
    s.setdefault("lookback_hours", 26)
    s.setdefault("max_items_per_topic", 8)
    s.setdefault("max_candidates_per_topic", 30)
    s.setdefault("timezone", "America/New_York")
    s.setdefault("require_date", True)
    if "topics" not in cfg or not cfg["topics"]:
        raise ValueError(f"{path}: no [topics.*] tables defined")
    if "feed" not in cfg or not cfg["feed"]:
        raise ValueError(f"{path}: no [[feed]] entries defined")
    for key, topic in cfg["topics"].items():
        topic.setdefault("label", key.replace("_", " ").title())
        topic["_regex"] = compile_keywords(topic.get("keywords", []))
    for feed in cfg["feed"]:
        for required in ("name", "url"):
            if required not in feed:
                raise ValueError(f"{path}: a [[feed]] entry is missing '{required}'")
        feed.setdefault("topic", "auto")
        feed.setdefault("weight", 1.0)
        if feed["topic"] != "auto" and feed["topic"] not in cfg["topics"]:
            raise ValueError(f"{path}: feed {feed['name']!r} has unknown topic {feed['topic']!r}")
    return cfg


def compile_keywords(keywords: list[str]) -> tuple[re.Pattern | None, re.Pattern | None]:
    """Two patterns: case-insensitive words/phrases, and case-sensitive short acronyms
    (e.g. "EV", "DOT", "COP") so that "cop" or "dot" in ordinary prose do not match."""
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


def classify(text: str, topics: dict) -> tuple[str, int]:
    """Best topic by keyword hits. Returns ("", 0) when nothing matches."""
    best_key, best_hits = "", 0
    for key, topic in topics.items():
        hits = keyword_hits(text, topic["_regex"])
        if hits > best_hits:
            best_key, best_hits = key, hits
    return best_key, best_hits


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
        # cut at the last sentence end or word boundary, whichever is later and reasonable
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


def parse_feed(feed_cfg: dict, cfg: dict, now: datetime, window_start: datetime) -> tuple[list[Item], FeedHealth]:
    """Fetch + parse one feed. Any exception is turned into a health record, so one
    misbehaving source can never abort the whole run."""
    try:
        return _parse_feed(feed_cfg, cfg, now, window_start)
    except Exception as exc:  # noqa: BLE001
        return [], FeedHealth(name=feed_cfg["name"], url=feed_cfg["url"], topic=feed_cfg["topic"],
                              status="error", error=f"{type(exc).__name__}: {exc}"[:200])


def _parse_feed(feed_cfg: dict, cfg: dict, now: datetime, window_start: datetime) -> tuple[list[Item], FeedHealth]:
    import feedparser  # lazy: keeps `send` usable in minimal environments

    health = FeedHealth(name=feed_cfg["name"], url=feed_cfg["url"], topic=feed_cfg["topic"])
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
            # An HTML page (login wall, 404 page, moved site) where a feed used to be.
            health.status = "parse-error"
            health.error = "response is not an RSS/Atom feed"
        elif parsed.get("bozo"):
            health.status = "parse-error"
            health.error = str(parsed.get("bozo_exception", "unparseable feed"))[:200]
        else:
            health.status = "empty"
        return [], health

    topics = cfg["topics"]
    require_date = bool(cfg["settings"].get("require_date", True))
    weight = float(feed_cfg.get("weight", 1.0))
    items: list[Item] = []
    for entry in entries:
        link = (entry.get("link") or "").strip()
        title = clean_text(entry.get("title") or "", limit=300)
        if not link or not title:
            continue
        published = entry_datetime(entry)
        if published is None:
            if require_date:
                continue
            published = now
        if published < window_start or published > now + timedelta(hours=2):
            continue
        health.items_in_window += 1
        summary = entry_summary(entry)
        text = f"{title}. {summary}"
        if feed_cfg["topic"] == "auto":
            topic, hits = classify(text, topics)
            if not topic:
                continue
        else:
            topic = feed_cfg["topic"]
            hits = keyword_hits(text, topics[topic]["_regex"])
        age_hours = (now - published).total_seconds() / 3600
        recency = 1.0 if age_hours <= 12 else 0.85 if age_hours <= 24 else 0.7
        score = round(weight * (1.0 + 0.35 * min(hits, 6)) * recency, 4)
        items.append(Item(
            id=item_id(link), title=title, url=link, source=feed_cfg["name"], topic=topic,
            published=published.astimezone(timezone.utc).isoformat(timespec="seconds"),
            summary=summary, score=score, keyword_hits=hits,
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


def collect(cfg: dict, hours: float | None = None, now: datetime | None = None,
            only_topics: list[str] | None = None) -> Collected:
    now = now or datetime.now(timezone.utc)
    hours = float(hours or cfg["settings"]["lookback_hours"])
    window_start = now - timedelta(hours=hours)
    feeds = cfg["feed"]
    if only_topics:
        feeds = [f for f in feeds if f["topic"] == "auto" or f["topic"] in only_topics]

    all_items: list[Item] = []
    health: list[FeedHealth] = []
    with cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(parse_feed, f, cfg, now, window_start): f for f in feeds}
        for fut in cf.as_completed(futures):
            items, h = fut.result()
            all_items.extend(items)
            health.append(h)
    health.sort(key=lambda h: (h.status != "ok", h.name.lower()))

    deduped = dedupe(all_items)
    cap = int(cfg["settings"]["max_candidates_per_topic"])
    topics_out = []
    for key, topic in cfg["topics"].items():
        if only_topics and key not in only_topics:
            continue
        chosen = sorted((i for i in deduped if i.topic == key), key=lambda i: i.score, reverse=True)[:cap]
        topics_out.append({"key": key, "label": topic["label"], "items": [asdict(i) for i in chosen]})

    stats = {
        "feeds": len(health),
        "feeds_ok": sum(1 for h in health if h.status == "ok"),
        "items_total": sum(h.items_total for h in health),
        "items_in_window": sum(h.items_in_window for h in health),
        "items_kept": len(deduped),
        "items_offered": sum(len(t["items"]) for t in topics_out),
    }
    return Collected(
        generated_at=now.isoformat(timespec="seconds"),
        window_start=window_start.isoformat(timespec="seconds"),
        lookback_hours=hours,
        timezone=cfg["settings"]["timezone"],
        topics=topics_out,
        feed_health=[asdict(h) for h in health],
        stats=stats,
    )


# --------------------------------------------------------------------------- summarizing

SYSTEM_PROMPT = """You compile a daily news digest for an academic economist whose research spans transportation, energy, environment, and industrial organization / antitrust.

You receive candidate items collected from RSS feeds in the last day, grouped by topic. Each item has an id, title, source, publication time, URL, and the feed's own blurb.

Select and summarize. Prefer substantive developments: policy and regulatory actions, market and price movements, enforcement cases, court decisions, major corporate decisions, new data releases, and notable research. Skip marketing, listicles, opinion with no news, and near-duplicates of a story already chosen (in any section). Balance sources; do not let one outlet dominate a section.

Ground every summary strictly in the provided title and blurb. Do not add facts, numbers, or names that are not in the input. If a blurb is thin, keep the summary short rather than speculating. Never invent items: every id you return must come from the input.

Write plainly. No hype, no em-dashes."""


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "top_line": {
            "type": "string",
            "description": "Three to five sentences on the day's most consequential developments across all sections.",
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "id copied verbatim from the input"},
                                "headline": {"type": "string", "description": "Concise headline; may lightly rephrase the title"},
                                "summary": {"type": "string", "description": "Two or three sentences grounded in the blurb"},
                                "why_it_matters": {"type": "string", "description": "One sentence on relevance to research or policy in this field; empty string if nothing non-obvious to say"},
                            },
                            "required": ["id", "headline", "summary", "why_it_matters"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["topic", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["top_line", "sections"],
    "additionalProperties": False,
}


def _candidates_for_prompt(collected: Collected) -> dict:
    out = {}
    for t in collected.topics:
        out[t["key"]] = [
            {"id": i["id"], "title": i["title"], "source": i["source"], "published": i["published"],
             "url": i["url"], "blurb": i["summary"]}
            for i in t["items"]
        ]
    return out


def map_claude_output(data: dict, collected: Collected, cfg: dict) -> dict:
    """Turn the model's {id, headline, summary, why_it_matters} picks into full digest
    sections, resolving each id against the collected items so URLs, sources, and
    timestamps come from the feed and never from the model."""
    by_id = {i["id"]: i for t in collected.topics for i in t["items"]}
    labels = {t["key"]: t["label"] for t in collected.topics}
    max_items = int(cfg["settings"]["max_items_per_topic"])
    picked_sections = {s.get("topic"): s.get("items", []) for s in data.get("sections", [])}
    seen: set[str] = set()
    sections = []
    for t in collected.topics:
        key = t["key"]
        items_out = []
        for pick in picked_sections.get(key, []):
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
            })
            if len(items_out) >= max_items:
                break
        sections.append({"topic": key, "label": labels.get(key, key), "items": items_out})
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
    max_items = int(cfg["settings"]["max_items_per_topic"])
    payload = _candidates_for_prompt(collected)
    if not any(payload.values()):
        return None
    user_text = (
        f"Date: {collected.generated_at}. Pick up to {max_items} items per section, ordered by importance. "
        f"Sections and their keys: " + ", ".join(f"{t['label']} = {t['key']}" for t in collected.topics) + ".\n\n"
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
    max_items = int(cfg["settings"]["max_items_per_topic"])
    sections = []
    for t in collected.topics:
        items = []
        for i in t["items"][:max_items]:
            items.append({
                "headline": i["title"], "url": i["url"], "source": i["source"],
                "published": i["published"], "summary": clean_text(i["summary"], 320),
                "why_it_matters": "",
            })
        sections.append({"topic": t["key"], "label": t["label"], "items": items})
    n = sum(len(s["items"]) for s in sections)
    top_line = (
        f"{n} items selected by keyword relevance from {collected.stats.get('feeds_ok', 0)} sources "
        f"over the last {collected.lookback_hours:g} hours. Summaries are the feeds' own blurbs. "
        "Set ANTHROPIC_API_KEY to get ranked, written summaries."
    )
    return {"top_line": top_line, "sections": sections, "generated_by": "keyword-fallback"}


def build_digest(collected: Collected, cfg: dict, use_ai: bool = True) -> dict:
    digest = None
    if use_ai and os.environ.get("ANTHROPIC_API_KEY"):
        digest = summarize_with_claude(collected, cfg)
    if digest is None:
        digest = summarize_fallback(collected, cfg)
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
        for k in ("topic", "items"):
            if k not in s:
                raise ValueError(f"section is missing '{k}': {s}")
        for i in s["items"]:
            for k in ("headline", "url"):
                if not i.get(k):
                    raise ValueError(f"item is missing '{k}': {i}")


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
    date = digest.get("date")
    try:
        d = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now(tz)
    except ValueError:
        d = datetime.now(tz)
    return d.strftime("%A, %B %d, %Y").replace(" 0", " ")


def subject_line(digest: dict, tz: ZoneInfo) -> str:
    labels = [s.get("label") or s["topic"] for s in digest["sections"] if s.get("items")]
    short = ", ".join(l.split(" &")[0].split(" /")[0] for l in labels) or "no new items"
    return f"News digest {digest_date_label(digest, tz)}: {short}"


def render_html(digest: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["settings"]["timezone"])
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{e(subject_line(digest, tz))}</title></head>",
        "<body style='margin:0;padding:0;background:#f4f4f2;font-family:Georgia,\"Times New Roman\",serif;color:#1f1f1f;'>",
        "<div style='max-width:680px;margin:0 auto;padding:24px 16px;background:#ffffff;'>",
        f"<h1 style='font-size:22px;margin:0 0 4px 0;'>Daily news digest</h1>",
        f"<div style='color:#666;font-size:13px;margin-bottom:18px;'>{e(digest_date_label(digest, tz))}"
        f" &middot; transportation, energy, environment, industrial organization</div>",
    ]
    if digest.get("top_line"):
        parts.append(
            "<div style='background:#f7f3e8;border-left:4px solid #b08a2e;padding:12px 14px;margin:0 0 22px 0;font-size:15px;line-height:1.5;'>"
            f"{e(digest['top_line'])}</div>"
        )
    for s in digest["sections"]:
        label = s.get("label") or s["topic"]
        parts.append(f"<h2 style='font-size:17px;border-bottom:1px solid #ddd;padding-bottom:4px;margin:26px 0 10px 0;'>{e(label)}</h2>")
        if not s.get("items"):
            parts.append("<p style='color:#777;font-size:14px;'>No items in the window.</p>")
            continue
        for i in s["items"]:
            meta = " &middot; ".join(x for x in (e(i.get("source", "")), e(fmt_time(i.get("published"), tz))) if x)
            parts.append("<div style='margin:0 0 16px 0;'>")
            parts.append(f"<a href='{e(i['url'], quote=True)}' style='font-size:15px;font-weight:bold;color:#1a3c6e;text-decoration:none;'>{e(i['headline'])}</a>")
            if meta:
                parts.append(f"<div style='color:#777;font-size:12px;margin:2px 0 4px 0;'>{meta}</div>")
            if i.get("summary"):
                parts.append(f"<div style='font-size:14px;line-height:1.5;'>{e(i['summary'])}</div>")
            if i.get("why_it_matters"):
                parts.append(f"<div style='font-size:13px;line-height:1.45;color:#444;font-style:italic;margin-top:3px;'>Why it matters: {e(i['why_it_matters'])}</div>")
            parts.append("</div>")
    # footer: feed health + provenance
    bad = [h for h in digest.get("feed_health", []) if h.get("status") != "ok"]
    stats = digest.get("stats", {})
    parts.append("<div style='margin-top:30px;padding-top:10px;border-top:1px solid #ddd;color:#888;font-size:12px;line-height:1.5;'>")
    if stats:
        parts.append(
            f"Sources: {stats.get('feeds_ok', 0)} of {stats.get('feeds', 0)} feeds returned items; "
            f"{stats.get('items_in_window', 0)} items in the last {digest.get('lookback_hours', '?')} hours, "
            f"{stats.get('items_kept', 0)} after topic filtering and de-duplication.<br>"
        )
    if bad:
        parts.append("Feeds with no items this run: " + ", ".join(
            f"{e(h['name'])} ({e(h['status'])}{': ' + e(h['error'][:60]) if h.get('error') else ''})" for h in bad
        ) + ".<br>")
    parts.append(f"Generated by /news-digest ({e(str(digest.get('generated_by', 'unknown')))}). "
                 "Sources and keywords: <code>scripts/news_digest/feeds.toml</code>.")
    parts.append("</div></div></body></html>")
    return "\n".join(parts)


def render_text(digest: dict, cfg: dict) -> str:
    tz = ZoneInfo(cfg["settings"]["timezone"])
    lines = [f"DAILY NEWS DIGEST — {digest_date_label(digest, tz)}", ""]
    if digest.get("top_line"):
        lines += [digest["top_line"], ""]
    for s in digest["sections"]:
        label = s.get("label") or s["topic"]
        lines += [label.upper(), "-" * len(label)]
        if not s.get("items"):
            lines += ["(no items in the window)", ""]
            continue
        for i in s["items"]:
            meta = " · ".join(x for x in (i.get("source", ""), fmt_time(i.get("published"), tz)) if x)
            lines.append(f"* {i['headline']}")
            if meta:
                lines.append(f"  {meta}")
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
            if i.get("why_it_matters"):
                lines.append(f"  Why it matters: {i['why_it_matters']}")
            lines.append(f"  {i['url']}")
            lines.append("")
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
    for s in digest["sections"]:
        lines += [f"## {s.get('label') or s['topic']}", ""]
        if not s.get("items"):
            lines += ["_No items in the window._", ""]
            continue
        for i in s["items"]:
            meta = " · ".join(x for x in (i.get("source", ""), fmt_time(i.get("published"), tz)) if x)
            lines.append(f"- **[{i['headline']}]({i['url']})** ({meta})" if meta else f"- **[{i['headline']}]({i['url']})**")
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
            if i.get("why_it_matters"):
                lines.append(f"  _Why it matters: {i['why_it_matters']}_")
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


# --------------------------------------------------------------------------- CLI

def cmd_collect(args) -> int:
    cfg = load_config(args.config)
    collected = collect(cfg, hours=args.hours, only_topics=args.topics)
    payload = asdict(collected)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"wrote {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    s = collected.stats
    log(f"feeds ok {s['feeds_ok']}/{s['feeds']}; items in window {s['items_in_window']}; "
        f"kept {s['items_kept']}; offered {s['items_offered']}")
    return 3 if s["feeds_ok"] == 0 else 0


def cmd_send(args) -> int:
    cfg = load_config(args.config)
    try:
        digest = json.loads(Path(args.digest).read_text(encoding="utf-8"))
        validate_digest(digest)
    except (OSError, ValueError) as exc:
        log(f"bad digest JSON: {exc}")
        return 2
    labels = {k: t["label"] for k, t in cfg["topics"].items()}
    for s in digest["sections"]:
        s.setdefault("label", labels.get(s["topic"], s["topic"]))
    digest.setdefault("generated_by", "claude-in-session")
    digest.setdefault("date", datetime.now(ZoneInfo(cfg["settings"]["timezone"])).strftime("%Y-%m-%d"))
    return deliver(digest, cfg, args.out, args.dry_run)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.only_at_hour is not None and not should_run_now(args.only_at_hour, cfg["settings"]["timezone"]):
        local = datetime.now(ZoneInfo(cfg["settings"]["timezone"])).strftime("%H:%M %Z")
        log(f"skipping: local time is {local}, not the {args.only_at_hour}:00 hour")
        return 0
    collected = collect(cfg, hours=args.hours, only_topics=args.topics)
    s = collected.stats
    log(f"feeds ok {s['feeds_ok']}/{s['feeds']}; items in window {s['items_in_window']}; "
        f"kept {s['items_kept']}; offered {s['items_offered']}")
    if s["feeds_ok"] == 0:
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
    collected = collect(cfg, hours=args.hours)
    width = max(len(h["name"]) for h in collected.feed_health) + 2
    print(f"{'feed':<{width}} {'status':<12} {'http':>5} {'total':>6} {'window':>7} {'kept':>5}  error")
    for h in collected.feed_health:
        print(f"{h['name']:<{width}} {h['status']:<12} {str(h['http_status'] or ''):>5} "
              f"{h['items_total']:>6} {h['items_in_window']:>7} {h['items_kept']:>5}  {h['error'][:70]}")
    s = collected.stats
    print(f"\n{s['feeds_ok']}/{s['feeds']} feeds ok; {s['items_in_window']} items in window; {s['items_kept']} kept")
    return 3 if s["feeds_ok"] == 0 else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", default=str(DEFAULT_CONFIG), help="feeds.toml path")

    sp = sub.add_parser("collect", help="fetch feeds and write candidate items JSON")
    common(sp)
    sp.add_argument("--hours", type=float, help="lookback window (default from config)")
    sp.add_argument("--topics", type=lambda s: [t.strip() for t in s.split(",") if t.strip()],
                    help="comma-separated topic keys to include")
    sp.add_argument("--out", help="write JSON here instead of stdout")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("send", help="render a digest JSON and email it (or write with --out)")
    common(sp)
    sp.add_argument("--digest", required=True, help="digest JSON produced by Claude or by `run --save-json`")
    sp.add_argument("--out", help="also write .html/.txt/.md files here (path to the .html)")
    sp.add_argument("--dry-run", action="store_true", help="render only; never send")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("run", help="collect + summarize + send")
    common(sp)
    sp.add_argument("--hours", type=float)
    sp.add_argument("--topics", type=lambda s: [t.strip() for t in s.split(",") if t.strip()])
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
