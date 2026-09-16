# News digest pipeline

Backs the `/news-digest` skill and the `Daily news digest` GitHub Actions workflow. Builds
an email that opens with the Washington region's traffic and transportation policy
(Northern Virginia first, at state, county, and city or town level, then the District,
Maryland, and region-wide bodies), continues with United States news on transportation,
energy, environment, and industrial organization, adds a few major world stories, and
closes with an archive section on the region's transportation history.

```
scripts/news_digest/
├── news_digest.py        the pipeline (collect / send / run / check-feeds / archive-plan)
├── feeds.toml            sources, topics, region + jurisdiction keywords, limits, archive schedule
├── archive.toml          curated landmarks (2002 to 2025) for the archive section
├── requirements.txt      feedparser, requests, anthropic (optional)
├── test_news_digest.py   offline tests (python3 -m unittest scripts/news_digest/test_news_digest.py)
└── README.md             this file
```

## How it fits together

| Piece | Runs where | Gathers with | Summarizes with | Sends with |
|---|---|---|---|---|
| `news_digest.py run` | GitHub Actions (daily cron) or your terminal | RSS feeds and Google News queries in `feeds.toml` | Claude API if `ANTHROPIC_API_KEY` is set, else keyword ranking + feed blurbs | SMTP |
| `/news-digest` skill | A Claude Code session (manual or a scheduled Routine) | `news_digest.py collect`, with WebSearch as fallback | Claude, in the session | `news_digest.py send` over SMTP, or an email connector, or saves a file |

The two share one contract: a **digest JSON** with `top_line` and `sections[].items[]`
(`headline`, `url`, `source`, `published`, `summary`, `why_it_matters`, and for region
items `jurisdiction` and `level`). `send` renders whatever produced it and merges the
day's archive landmarks.

## How items are routed

1. **Topic** from the `[topics]` keyword lists (or fixed per feed).
2. **Region** from the `[regions]` place keywords (Northern Virginia, District, Maryland,
   region-wide). A story goes to a region section only when it is also about transport or
   traffic (`traffic_keywords`); a Maryland utility rate case stays under US energy. Local
   feeds (`region = "dc"`) fall back to their `subregion` when no place resolves, and drop
   items that are not about roads, rail, transit, or streets.
3. **Jurisdiction tag** for region items from the `[[jurisdiction]]` entries: the most
   specific level with any hit wins (town or city, then county, then state, then regional
   bodies), so a VDOT project in Fairfax County reads "Fairfax County (county)".
4. **Score** = feed weight × region boost × keyword hits × recency; duplicates across feeds
   collapse to the best copy. Google News items carry the real outlet as their source.

## The archive

`[archive]` in `feeds.toml` schedules it statelessly from `anchor` (the first day it ran):

- Days 1 to `landmark_days`: the tier-1 landmarks from `archive.toml`, in date order, split
  evenly. Each links to a dated Google search for contemporary coverage; the entries were
  written from general knowledge and are marked "details to verify" when confidence is
  medium, so confirm before citing.
- Afterwards: one historical week per day from `start_week` (January 2016) forward, fetched
  as a Google News RSS query with `after:` / `before:` operators and summarized like live
  news, merged with any landmark dated in that week. `weeks_per_day` and `order` adjust
  the pace and direction.
- When the schedule reaches the present: landmarks from this calendar week in earlier
  years.

Preview with `python3 scripts/news_digest/news_digest.py archive-plan --days 10 --verbose`.
Replay a week with `--archive-week 2017-12-04` on `run` or `send`.

## One-time setup for email

1. Pick a sending mailbox. Gmail is the least friction: turn on 2-step verification, then
   create an **app password** (Google Account → Security → App passwords). Use that
   16-character password, never the account password. Any SMTP relay works the same way
   (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`).
2. For the scheduled GitHub run, add these repository secrets under
   *Settings → Secrets and variables → Actions*: `SMTP_HOST`, `SMTP_USERNAME`,
   `SMTP_PASSWORD`, `DIGEST_TO`, and optionally `SMTP_PORT`, `DIGEST_FROM`,
   `ANTHROPIC_API_KEY`.
3. The workflow only fires on a schedule from the default branch. After merging, use
   *Actions → Daily news digest → Run workflow* with `dry_run` ticked to see a rendered
   digest as an artifact, then untick it to send a real one.

For local runs, export the same variables in your shell.

## Everyday commands

```bash
# Health of every feed (HTTP status, items, parse errors)
python3 scripts/news_digest/news_digest.py check-feeds

# Render without sending; writes digest.html, digest.txt, digest.md
python3 scripts/news_digest/news_digest.py run --dry-run --out /tmp/digest.html

# Full run: collect, summarize, email
python3 scripts/news_digest/news_digest.py run

# Only candidates, for a Claude session to rank and summarize
python3 scripts/news_digest/news_digest.py collect --out /tmp/items.json

# Email a digest JSON that Claude wrote (today's landmarks are merged in)
python3 scripts/news_digest/news_digest.py send --digest /tmp/digest.json

# What the archive section carries over the next ten days
python3 scripts/news_digest/news_digest.py archive-plan --days 10 --verbose
```

Useful flags on `run`: `--hours N` (window), `--sections dc,us_energy` (subset by group or
key), `--no-ai` (skip the API), `--save-json path`, `--no-archive`, `--archive-week
YYYY-MM-DD`, `--only-at-hour 10` (exit quietly unless the local hour in the configured
timezone is 10; used by the cron guard).

## Tuning

Everything lives in `feeds.toml` and `archive.toml`:

- **Sources**: add `[[feed]]` entries with a `url`, or a `google_news` query for outlets
  without feeds and for place-based coverage. `region = "dc"` plus `subregion` marks local
  sources; `region = "world"` feeds only ever reach the world section.
- **Keywords**: `[topics.*]`, `[regions.*]` (with `exclude` lists), `[[jurisdiction]]`, and
  `traffic_keywords`. Short all-caps entries (`EV`, `DC`, `COP`) match case-sensitively.
- **Limits**: `[limits]` per section key; `dc_nova` gets the most room.
- **Landmarks**: edit `archive.toml`; add a confirmed `url` to replace the search link.

The email footer names feeds that returned nothing in the current run. Feed URLs were
assembled from publishers' standard feed paths and should be confirmed with `check-feeds`
on the first run from a machine with open network access; prune any that stay dead.

## Claude API usage

With `ANTHROPIC_API_KEY` set, `run` sends the candidate items (id, title, source, time, URL,
blurb, jurisdiction) per section to the model with a JSON schema for the output. The model
picks and summarizes; the script maps each returned id back to the collected item, so URLs,
sources, and timestamps always come from the feed, never from the model. Defaults:
`DIGEST_MODEL=claude-opus-5`, `DIGEST_EFFORT=medium`. Any API failure, including a safety
refusal, falls back to keyword ranking so the email still goes out. A typical day is
roughly 50k input tokens.
