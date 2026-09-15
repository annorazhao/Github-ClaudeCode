---
name: news-digest
description: Build and email a daily news digest on transportation, energy, environment, and industrial organization / antitrust. Collects the last day's items from RSS feeds via scripts/news_digest/news_digest.py (WebSearch as fallback when the sandbox blocks feed hosts), has Claude select and summarize them by section, and delivers over SMTP, an email connector, or a saved file. Invoke explicitly as /news-digest, or from the daily Routine / GitHub Actions cron at 10:00 America/New_York.
argument-hint: "[--send | --preview] [--hours N] [--topics key,key]"
allowed-tools: ["Read", "Write", "Bash", "Glob", "WebSearch", "WebFetch"]
effort: medium
disable-model-invocation: true
---

# /news-digest — Daily News Digest

Gather what happened in the last day across four fields, select what matters, write short
grounded summaries, and email the result. Runs on its own every morning; runs on demand
for a preview or a custom window.

**Input:** `$ARGUMENTS`

| Flag | Meaning |
|------|---------|
| `--send` | Deliver the digest (email). This is what the schedule passes. |
| `--preview` | Default when invoked by hand: render and show the digest, send nothing. |
| `--hours` | Lookback window in hours (default `26` from `feeds.toml`). |
| `--topics` | Comma-separated subset of `transportation,energy,environment,industrial_organization`. |

The topics, sources, keywords, and limits live in `scripts/news_digest/feeds.toml`. The
pipeline itself is `scripts/news_digest/news_digest.py` (see its README for setup).

---

## Instructions

### Step 1: Parse arguments

- No flag → preview mode. `--send` → deliver. Record `--hours` and `--topics` if given.
- Today's date in `America/New_York` names the output files: `quality_reports/news_digests/YYYY-MM-DD.*`
  (the directory is gitignored).

### Step 2: Collect candidates

```bash
python3 -c "import feedparser, requests" 2>/dev/null || \
  python3 -m pip install --quiet -r scripts/news_digest/requirements.txt
mkdir -p quality_reports/news_digests
python3 scripts/news_digest/news_digest.py collect \
  --out quality_reports/news_digests/YYYY-MM-DD.items.json [--hours N] [--topics a,b]
```

Read the JSON. It has `topics[]` (each with `key`, `label`, and ranked candidate `items[]`),
`feed_health[]`, and `stats`. Each item carries `id`, `title`, `url`, `source`, `published`
(UTC ISO), `summary` (the feed's blurb, HTML stripped), `score`, `keyword_hits`.

**If the exit code is 3** (every feed failed; the health list shows `HTTP 403` or connection
errors on every row), the environment blocks outbound fetches. Switch to the WebSearch
fallback:

1. For each topic, run two or three WebSearch queries phrased for the last day, for example
   "transportation policy news", "freight rail trucking news this week", "electricity market
   FERC news", "EPA rule climate news", "FTC DOJ antitrust merger news". Prefer the domains
   listed in `feeds.toml` by passing them as `allowed_domains` on at least one query per topic.
2. Build the same candidate structure by hand: `title`, `url`, `source` (the site name),
   `published` (only if the result shows a date; otherwise omit), `summary` (the result
   snippet). Drop anything visibly older than the window.
3. Say in the top line that items came from web search rather than the feed list, since
   search results carry less reliable timestamps.

### Step 3: Select and summarize

You are the summarizer. Work from the candidates only.

- Choose up to `max_items_per_topic` (8) per section, ordered by importance. Prefer policy
  and regulatory actions, enforcement cases and court decisions, market and price moves,
  major corporate decisions, data releases, and notable research. Skip marketing, listicles,
  bare opinion, and near-duplicates of a story already chosen in any section. Do not let
  one outlet dominate a section.
- **Ground every sentence in the candidate's title and blurb.** Add no facts, numbers,
  names, or context that are not in the input. A thin blurb gets a one-line summary, not a
  guess. Copy `url`, `source`, and `published` verbatim from the collected item.
- Write a `top_line` of three to five sentences on the day's most consequential developments
  across sections, and for each item a two or three sentence `summary` plus a one-sentence
  `why_it_matters` for a researcher in that field (empty string if there is nothing
  non-obvious to say). Plain prose, no hype, no dashes.

Write `quality_reports/news_digests/YYYY-MM-DD.json`:

```json
{
  "top_line": "…",
  "sections": [
    {"topic": "transportation", "items": [
      {"headline": "…", "url": "…", "source": "…", "published": "2026-09-15T13:02:00+00:00",
       "summary": "…", "why_it_matters": "…"}
    ]},
    {"topic": "energy", "items": []},
    {"topic": "environment", "items": []},
    {"topic": "industrial_organization", "items": []}
  ]
}
```

Section `label`, `date`, and provenance are filled in by the script. Keep every section
present even when empty, in the order above.

### Step 4: Deliver

**Preview** (default):

```bash
python3 scripts/news_digest/news_digest.py send \
  --digest quality_reports/news_digests/YYYY-MM-DD.json \
  --out quality_reports/news_digests/YYYY-MM-DD.html --dry-run
```

Then show the user the Markdown twin the script wrote (`YYYY-MM-DD.md`).

**Send** (`--send`), in this order, stopping at the first path that works:

1. SMTP through the script (needs `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_TO`
   in the environment; `SMTP_PORT`, `SMTP_SECURITY`, `DIGEST_FROM` optional):

   ```bash
   python3 scripts/news_digest/news_digest.py send \
     --digest quality_reports/news_digests/YYYY-MM-DD.json \
     --out quality_reports/news_digests/YYYY-MM-DD.html
   ```

   Exit 0 means delivered. Exit 2 means the settings are missing; move on.
2. An email connector attached to this session (Gmail or similar): send the `.html` as the
   body with the subject from the first line of the `.txt`, to the user's address.
3. Neither available: end the turn with the **complete digest in Markdown as the final
   message**, preceded by one sentence stating that email is not configured. A scheduled
   Routine forwards that final message to the owner's inbox as its run notification, so the
   digest still arrives.

### Step 5: Report

One short paragraph: how it was delivered and to whom, how many items per section, which
feeds returned nothing (from `feed_health`), and where the files were saved. If the
WebSearch fallback was used, say so.

---

## Scheduling

Two schedulers exist so the digest arrives even when one is unavailable. Both target
10:00 America/New_York.

| Scheduler | Where it runs | Gathers | Summarizes | Sends | Setup |
|-----------|---------------|---------|------------|-------|-------|
| `.github/workflows/news-digest.yml` | GitHub Actions, daily cron (14:00 and 15:00 UTC with a local-hour guard, so exactly one fires at 10:00 local across daylight-saving changes) | RSS feeds | Claude API if `ANTHROPIC_API_KEY` secret is set, else keyword ranking | SMTP | Repository secrets listed in the workflow header; must be on the default branch |
| Claude Code Routine | A fresh cloud session at `0 14 * * *` UTC (10:00 EDT; 09:00 EST after November until the cron is adjusted) | This skill (RSS, or WebSearch when fetches are blocked) | Claude in the session | SMTP if the environment carries the variables, else a connector, else the run-notification email | Manage under claude.ai → Routines |

GitHub may delay scheduled workflows by several minutes and disables schedules on public
repositories with no commits for 60 days. The Routine consumes session usage on each run.

---

## Examples

### Example 1: Morning preview

**User says:** `/news-digest`
**Actions:**
1. `collect` pulls 32 feeds; 3 return nothing, 118 items fall in the window, 71 survive
   topic filtering and de-duplication.
2. Claude picks 6 to 8 items per section and writes grounded summaries into the JSON.
3. `send --dry-run --out` renders HTML, text, and Markdown; the Markdown is shown in chat
   with the three silent feeds named at the end.
**Result:** A reviewable digest, nothing emailed.

### Example 2: Scheduled send with blocked network

**Context:** The Routine fires in a sandbox whose egress policy blocks news hosts.
**Actions:**
1. `collect` exits 3 with `HTTP 403` on every feed.
2. WebSearch fallback gathers candidates per topic, restricted to the feed domains where
   possible.
3. SMTP variables are absent and no connector is attached, so the turn ends with the
   full Markdown digest and a one-line note that email is not configured.
**Result:** The Routine's notification email carries the digest.

### Example 3: Narrow window on two topics

**User says:** `/news-digest --preview --hours 72 --topics energy,environment`
**Result:** A three-day digest with two sections, rendered but not sent.

---

## Troubleshooting

**Error:** `collect` exits 3, every feed `HTTP 403` or `error`
**Cause:** The environment's network policy blocks outbound fetches (the 403 comes from the
egress proxy, not the publisher).
**Solution:** Use the WebSearch fallback in Step 2, or run from GitHub Actions where the
network is open. Check `python3 scripts/news_digest/news_digest.py check-feeds` from a
machine with normal access to see real feed health.

**Error:** `cannot send: missing email settings`
**Cause:** SMTP variables are not set in this environment.
**Solution:** Export `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_TO` (Gmail: use
an app password with 2-step verification on), or set them as repository secrets for the
workflow. See `scripts/news_digest/README.md`.

**Error:** `smtplib.SMTPAuthenticationError: (535, ...)`
**Cause:** Gmail rejects account passwords for SMTP; only app passwords work.
**Solution:** Create an app password under Google Account → Security → App passwords.

**Error:** One feed always shows `http-error` or `parse-error` in the footer
**Cause:** The publisher moved or removed the feed.
**Solution:** Fix or remove the `[[feed]]` entry in `feeds.toml`; `check-feeds` confirms.

**Symptom:** A section is empty most days
**Cause:** Too few sources for that topic, or a window that ends before US publishers post.
**Solution:** Add feeds under that topic in `feeds.toml`, or widen `lookback_hours`.

**Symptom:** The scheduled workflow never runs
**Cause:** Schedules fire only from the default branch, and only after the workflow file
has been merged there.
**Solution:** Merge, then trigger once by hand from the Actions tab to confirm the secrets.
