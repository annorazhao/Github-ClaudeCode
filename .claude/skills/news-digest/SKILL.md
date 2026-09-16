---
name: news-digest
description: Build and email a daily news digest that opens with Washington-region traffic and transportation policy (Northern Virginia at state, county, and city level, then the District, Maryland, and WMATA), continues with United States news on transportation, energy, environment, and industrial organization / antitrust, adds a few major world stories, and closes with an archive section on the region's transportation history. Collects via scripts/news_digest/news_digest.py (RSS + Google News; WebSearch as fallback when the sandbox blocks feed hosts), has Claude select and summarize per section, and delivers over SMTP, an email connector, or a saved file. Invoke explicitly as /news-digest, or from the daily Routine / GitHub Actions cron at 10:00 America/New_York.
argument-hint: "[--send | --preview] [--hours N] [--sections dc,us_energy,archive]"
allowed-tools: ["Read", "Write", "Bash", "Glob", "WebSearch", "WebFetch"]
effort: medium
disable-model-invocation: true
---

# /news-digest — Daily News Digest

Gather what happened in the last day, select what matters, write short grounded
summaries, and email the result. Runs on its own every morning; runs on demand for a
preview or a custom window.

**Input:** `$ARGUMENTS`

| Flag | Meaning |
|------|---------|
| `--send` | Deliver the digest (email). This is what the schedule passes. |
| `--preview` | Default when invoked by hand: render and show the digest, send nothing. |
| `--hours` | Lookback window in hours (default `26` from `feeds.toml`). |
| `--sections` | Comma-separated subset: groups (`dc`, `us`, `world`, `archive`) or keys (`dc_nova`, `us_energy`, …). |

Sources, region and jurisdiction keywords, per-section limits, and the archive schedule
live in `scripts/news_digest/feeds.toml`; curated landmarks in `archive.toml`. The
pipeline is `scripts/news_digest/news_digest.py` (see its README for setup).

## The digest's shape

| Group | Sections (keys) | Cap |
|-------|-----------------|-----|
| Washington region: traffic and transportation policy | Northern Virginia `dc_nova` (tagged by jurisdiction: town, city, county, state, NVTA/NVTC/VRE), District of Columbia `dc_dc`, Maryland `dc_maryland`, Region-wide and Metro `dc_regional` | 10 / 5 / 5 / 5 |
| United States | `us_transportation`, `us_energy`, `us_environment`, `us_industrial_organization` | 5 each |
| World | `world` (a splash of major stories, for research context) | 4 |
| From the archive | `archive`: one past year per email, starting with last year and moving back to 2016 (curated landmarks plus policy-ranked searched coverage, at least ten items when the sources allow), then a policy-roots chunk, then one quarter per email | 14 |

Northern Virginia is the priority. Prefer policy actions, project milestones, funding
and tolling decisions, enforcement changes, and data releases at the state, county, and
city or town level over routine incident reports.

---

## Instructions

### Step 1: Parse arguments

- No flag → preview mode. `--send` → deliver. Record `--hours` and `--sections` if given.
- Today's date in `America/New_York` names the output files: `quality_reports/news_digests/YYYY-MM-DD.*`
  (the directory is gitignored).

### Step 2: Collect candidates

```bash
python3 -c "import feedparser, requests" 2>/dev/null || \
  python3 -m pip install --quiet -r scripts/news_digest/requirements.txt
mkdir -p quality_reports/news_digests
python3 scripts/news_digest/news_digest.py collect \
  --out quality_reports/news_digests/YYYY-MM-DD.items.json [--hours N] [--sections a,b]
```

Read the JSON. It has `sections[]` (each with `key`, `label`, `group`, and ranked candidate
`items[]`), `archive` (today's plan: `phase`, `title`, `note`, and ready-made
`landmark_items[]`), `feed_health[]`, and `stats`. Each item carries `id`, `title`, `url`,
`source` (the real outlet, also for Google News items), `published` (UTC ISO), `summary`
(the feed's blurb, HTML stripped), `score`, `region`, `jurisdiction`, `level`.

**If the exit code is 3** (every feed failed; the health list shows `HTTP 403` or connection
errors on every row), the environment blocks outbound fetches. Switch to the WebSearch
fallback:

1. Region first. Run two or three searches per region section, phrased for the last day
   and naming places: "Fairfax County transportation news", "Arlington Alexandria Loudoun
   Prince William roads transit", "VDOT Commonwealth Transportation Board Northern
   Virginia", "WMATA Metro news", "DDOT D.C. Council traffic", "Maryland MDOT Purple Line
   I-270". Pass local outlets as `allowed_domains` on at least one query per section:
   wtop.com, washingtonpost.com, arlnow.com, ffxnow.com, alxnow.com, ggwash.org,
   insidenova.com, fairfaxtimes.com, loudountimes.com, potomaclocal.com, virginiamercury.com,
   marylandmatters.org, wamu.org, wusa9.com, nbcwashington.com, bizjournals.com.
2. Then one or two searches per US topic and one for the world section.
3. If today's archive phase is `period` (see `archive.title` and `archive.label`, e.g.
   "2025"), search that period for major policy developments: "Northern Virginia
   transportation policy <YYYY>", "Fairfax County Board transportation <YYYY>", "VDOT
   General Assembly toll <YYYY>", "WMATA Metro funding <YYYY>", "DDOT MDOT Purple Line
   <YYYY>", restricted to the local domains above; keep results dated in that period and
   aim for at least `archive.min_items` items, favoring decisions, funding, openings, and
   enforcement changes over incidents.
4. Build the same candidate structure by hand: `title`, `url`, `source` (site name),
   `published` (only if the result shows a date), `summary` (the snippet), and for region
   items a `jurisdiction` such as "Fairfax County" or "Virginia" with its `level`.
5. Say in the top line that items came from web search rather than the feed list, since
   search results carry less reliable timestamps.

### Step 3: Select and summarize

You are the summarizer. Work from the candidates only.

- Choose up to each section's cap, ordered by importance, and keep every section key even
  when it is empty. For the region sections, prefer concrete policy actions, project
  milestones, funding and tolling decisions, enforcement changes, and data releases; a
  single crash is rarely worth including unless it changed policy or closed a corridor for
  a long time. For the US sections, prefer policy and regulatory actions, enforcement cases
  and court decisions, market and price moves, major corporate decisions, data releases, and
  notable research. Skip marketing, listicles, bare opinion, and near-duplicates of a story
  already chosen in any section. Do not let one outlet dominate a section.
- **Ground every sentence in the candidate's title and blurb.** Add no facts, numbers,
  names, or context that are not in the input. A thin blurb gets a one-line summary, not a
  guess. Copy `url`, `source`, `published`, `jurisdiction`, and `level` verbatim from the
  collected item.
- For the archive period's items, pick the most consequential policy developments (at least
  `archive.min_items` when the candidates allow, ordered by importance), and write each as a
  short retrospective in the past tense naming the month and year. Landmarks are already
  written; leave them to the script (Step 4 merges them first and tops the section up).
- Write a `top_line` of three to five sentences on the day's most consequential
  developments, leading with the region when it has substantive news; for each item a two
  or three sentence `summary` plus a one-sentence `why_it_matters` for a researcher in that
  field (empty string if there is nothing non-obvious to say). Plain prose, no hype, no
  dashes.

Write `quality_reports/news_digests/YYYY-MM-DD.json`:

```json
{
  "top_line": "…",
  "sections": [
    {"section": "dc_nova", "items": [
      {"headline": "…", "url": "…", "source": "FFXnow", "published": "2026-09-15T13:02:00+00:00",
       "jurisdiction": "Fairfax County", "level": "county",
       "summary": "…", "why_it_matters": "…"}
    ]},
    {"section": "dc_dc", "items": []},
    {"section": "dc_maryland", "items": []},
    {"section": "dc_regional", "items": []},
    {"section": "us_transportation", "items": []},
    {"section": "us_energy", "items": []},
    {"section": "us_environment", "items": []},
    {"section": "us_industrial_organization", "items": []},
    {"section": "world", "items": []},
    {"section": "archive", "items": []}
  ]
}
```

Section labels, groups, the date, provenance, and today's landmarks are filled in by the
script.

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

One short paragraph: how it was delivered and to whom, how many items per group, which
archive chunk was included, which feeds returned nothing (from `feed_health`), and where
the files were saved. If the WebSearch fallback was used, say so.

---

## Scheduling

Two schedulers exist so the digest arrives even when one is unavailable. Both target
10:00 America/New_York.

| Scheduler | Where it runs | Gathers | Summarizes | Sends | Setup |
|-----------|---------------|---------|------------|-------|-------|
| `.github/workflows/news-digest.yml` | GitHub Actions, daily cron (14:00 and 15:00 UTC with a local-hour guard, so exactly one fires at 10:00 local across daylight-saving changes) | RSS + Google News feeds | Claude API if `ANTHROPIC_API_KEY` secret is set, else keyword ranking | SMTP | Repository secrets listed in the workflow header; must be on the default branch |
| Claude Code Routine | A fresh cloud session at `0 14 * * *` UTC (10:00 EDT; 09:00 EST after November until the cron is adjusted) | This skill (feeds, or WebSearch when fetches are blocked) | Claude in the session | SMTP if the environment carries the variables, else a connector, else the run-notification email | Manage under claude.ai → Routines |

The archive schedule is stateless: day number since `[archive].anchor` in `feeds.toml`
decides the chunk (day 0 is last year, then each earlier year to 2016, then policy roots,
then one quarter per day), so both schedulers show the same chunk on the same day. Preview
it with `python3 scripts/news_digest/news_digest.py archive-plan --days 12 --verbose`.

GitHub may delay scheduled workflows by several minutes and disables schedules on public
repositories with no commits for 60 days. The Routine consumes session usage on each run.

---

## Examples

### Example 1: Morning preview

**User says:** `/news-digest`
**Actions:**
1. `collect` pulls about 60 feeds and queries; a few return nothing; 140 items fall in the
   window, 80 survive region and topic filtering and de-duplication.
2. Claude picks up to 10 Northern Virginia items (tagged Fairfax County, Arlington County,
   Alexandria, Virginia, and so on), a handful each for the District, Maryland, and WMATA,
   five per US topic, and up to four world items, and writes grounded summaries.
3. `send --dry-run --out` renders HTML, text, and Markdown and merges today's landmarks
   into the archive section; the Markdown is shown in chat.
**Result:** A reviewable digest, nothing emailed.

### Example 2: Scheduled send with blocked network

**Context:** The Routine fires in a sandbox whose egress policy blocks news hosts.
**Actions:**
1. `collect` exits 3 with `HTTP 403` on every feed.
2. WebSearch fallback gathers candidates per section, local domains first, plus the
   archive period when the phase is `period`.
3. SMTP variables are absent and no connector is attached, so the turn ends with the
   full Markdown digest and a one-line note that email is not configured.
**Result:** The Routine's notification email carries the digest.

### Example 3: Region only, three days

**User says:** `/news-digest --preview --hours 72 --sections dc,archive`
**Result:** A three-day digest with the four region sections and the archive, rendered
but not sent.

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
**Solution:** Fix or remove the `[[feed]]` entry in `feeds.toml`, or replace it with a
`google_news` query; `check-feeds` confirms.

**Symptom:** A region section is empty most days, or items land in the wrong section
**Cause:** Missing place keywords, or a story that mentions a place without being about
transport (those go to the US topic sections by design).
**Solution:** Extend `[regions.*].keywords`, `[[jurisdiction]]` entries, or
`traffic_keywords` in `feeds.toml`.

**Symptom:** The archive shows the wrong chunk, or restarted from last year
**Cause:** `[archive].anchor` was changed, or the run used `--archive-day`.
**Solution:** Keep `anchor` fixed at the first day the archive ran; use `--archive-period
2019` (or `2019-Q4`) for a one-off replay.

**Symptom:** The scheduled workflow never runs
**Cause:** Schedules fire only from the default branch, and only after the workflow file
has been merged there.
**Solution:** Merge, then trigger once by hand from the Actions tab to confirm the secrets.
