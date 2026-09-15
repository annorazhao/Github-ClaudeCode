# News digest pipeline

Backs the `/news-digest` skill and the `Daily news digest` GitHub Actions workflow. Pulls
RSS/Atom feeds on transportation, energy, environment, and industrial organization,
ranks and summarizes the last day's items, renders an email, and sends it over SMTP.

```
scripts/news_digest/
├── news_digest.py        the pipeline (collect / send / run / check-feeds)
├── feeds.toml            sources, topics, keywords, settings
├── requirements.txt      feedparser, requests, anthropic (optional)
├── test_news_digest.py   offline tests (python3 -m unittest scripts/news_digest/test_news_digest.py)
└── README.md             this file
```

## How it fits together

| Piece | Runs where | Gathers with | Summarizes with | Sends with |
|---|---|---|---|---|
| `news_digest.py run` | GitHub Actions (daily cron) or your terminal | RSS feeds in `feeds.toml` | Claude API if `ANTHROPIC_API_KEY` is set, else keyword ranking + feed blurbs | SMTP |
| `/news-digest` skill | A Claude Code session (manual or a scheduled Routine) | `news_digest.py collect`, with WebSearch as fallback | Claude, in the session | `news_digest.py send` over SMTP, or an email connector, or saves a file |

The two share one contract: a **digest JSON** with `top_line` and `sections[].items[]`
(`headline`, `url`, `source`, `published`, `summary`, `why_it_matters`). `send` renders
whatever produced it.

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

# Email a digest JSON that Claude wrote
python3 scripts/news_digest/news_digest.py send --digest /tmp/digest.json
```

Useful flags on `run`: `--hours N` (window), `--topics energy,environment` (subset),
`--no-ai` (skip the API), `--save-json path`, `--only-at-hour 10` (exit quietly unless the
local hour in the configured timezone is 10; used by the cron guard).

## Tuning

Everything lives in `feeds.toml`:

- **Add or remove sources** as `[[feed]]` entries. `topic = "auto"` classifies each item by
  keyword and drops non-matching ones, which is right for general business feeds; a fixed
  topic keeps every item from that feed.
- **Keywords** per topic drive both classification of `auto` feeds and the relevance score.
  Short all-caps entries (`EV`, `DOT`, `COP`) match case-sensitively so ordinary words do
  not trigger them.
- **`weight`** nudges a source up or down in the ranking.
- **`max_items_per_topic`**, **`lookback_hours`**, **`timezone`** under `[settings]`.

The email footer names feeds that returned nothing in the current run, so dead URLs are
visible without reading logs. Feeds were assembled from publishers' standard feed paths and
should be confirmed with `check-feeds` on the first run from a machine with open network
access; prune any that stay dead.

## Claude API usage

With `ANTHROPIC_API_KEY` set, `run` sends the candidate items (id, title, source, time, URL,
blurb) to the model with a JSON schema for the output. The model picks and summarizes; the
script maps each returned id back to the collected item, so URLs, sources, and timestamps
always come from the feed, never from the model. Defaults: `DIGEST_MODEL=claude-opus-5`,
`DIGEST_EFFORT=medium`. Any API failure, including a safety refusal, falls back to keyword
ranking so the email still goes out. A typical day is roughly 40k input tokens.
