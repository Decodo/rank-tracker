# Rank Tracker

[![tests](https://github.com/triposat/rank-tracker/actions/workflows/test.yml/badge.svg)](https://github.com/triposat/rank-tracker/actions/workflows/test.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-hosted SEO rank tracker built on the [Decodo SERP Scraping API](https://decodo.com/scraping/serp). It tracks organic position, AI Overview citations, featured snippets, and PAA presence across keywords, locations, and devices, storing everything in SQLite and printing trend reports from the terminal.

## Quick start

> Examples below use a Linux / macOS shell. On Windows, replace *source .venv/bin/activate* with *.venv\Scripts\activate* and *cp* with *copy*.

```bash
# 1. Clone and enter the repo
git clone https://github.com/triposat/rank-tracker.git
cd rank-tracker

# 2. Create a virtualenv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Provide credentials
# (Sign up at https://decodo.com/scraping/serp if you don't have an account.)
cp .env.example .env
# Edit .env and paste the "Basic authentication token" from your Decodo dashboard
# (Web Scraping API → API Playground → top-right auth field)

# 4. Smoke-test the setup
python main.py doctor

# 5. Configure your keyword list
cp keywords.csv.example keywords.csv
# Replace the example rows with your own keywords, then run a check
python main.py check
```

## Sample outputs

All 4 samples below are captured from 1 continuous run against *keywords.csv.example*: *doctor* first, then *check* (Run 1), then *report*, then the heartbeat for Run 1. Your positions and scores will differ (SERPs change run-to-run).

*python main.py doctor* should print:

```
[OK]     DECODO_AUTH is set
[OK]     Database is writable at rank_tracker.db
[OK]     Decodo API reachable – got 4 organic results for 'decodo' (decodo.com pos=1)
[INFO]   No completed runs yet – schedule a check.
[INFO]   Lifetime Decodo API calls: 0
---
All checks passed. You're ready to run `python main.py check`.
```

If *doctor* reports *[FAIL]   Credentials rejected*, the token paste is the most common culprit, so check it for trailing whitespace and confirm you copied the pre-encoded *Basic authentication token* field rather than the raw username/password (the dashboard exposes both).

*python main.py check* (Run 1, fresh DB) prints:

```
Run 1 – skipping 1 of 8 (active/frequency rules)
Run 1 started – 7 check(s) due, concurrency=4
[1/7] 'best laptop 2026' @ United States/desktop -> pos=2 prev=None score=73.7 (AI-cited=False FS-owned=False PAA=True)
[2/7] 'best laptop 2026' @ United Kingdom/desktop -> pos=None prev=None score=28.0 (AI-cited=True FS-owned=False PAA=True)
[3/7] 'best laptop 2026' @ United States/mobile -> pos=5 prev=None score=47.7 (AI-cited=False FS-owned=False PAA=True)
[4/7] 'serp scraping api' @ United States/desktop -> pos=None prev=None score=3.0 (AI-cited=False FS-owned=False PAA=True)
[5/7] 'serp scraping api' @ Germany/desktop -> pos=None prev=None score=3.0 (AI-cited=False FS-owned=False PAA=True)
[6/7] 'how to scrape google' @ United Kingdom/desktop -> pos=None prev=None score=3.0 (AI-cited=False FS-owned=False PAA=True)
[7/7] 'residential proxies' @ United States/desktop -> pos=None prev=None score=0.0 (AI-cited=False FS-owned=False PAA=False)
Run 1 complete – 7 API call(s) this run, 7 lifetime (1 skipped by frequency rules)
```

*python main.py report* (after Run 1) prints:

```
Keyword                          Geo/Device                    Pos      Δ vs prev   Score   AI   FS   PAA
---------------------------------------------------------------------------------------------------------
best laptop 2026                 United Kingdom/desktop          –                   28.0    ✓    ·     ✓
best laptop 2026                 United States/desktop           2            NEW    73.7    ·    ·     ✓
best laptop 2026                 United States/mobile            5            NEW    47.7    ·    ·     ✓
how to scrape google             United Kingdom/desktop          –                    3.0    ·    ·     ✓
residential proxies              United States/desktop           –                    0.0    ·    ·     ·
serp scraping api                Germany/desktop                 –                    3.0    ·    ·     ✓
serp scraping api                United States/desktop           –                    3.0    ·    ·     ✓
```

Positions shown as *–* mean "not in the tracked top-N" (the keyword isn't ranking, or your domain isn't in the captured results).

## Commands

| Command | What it does |
|---|---|
| *python main.py doctor* | Verifies *DECODO_AUTH*, DB writability, live Decodo API, **last-run freshness**, and lifetime API call count. *--skip-api* skips the network call. *--max-staleness-hours N* warns if no successful run in N hours (default 48). |
| *python main.py test-alert* | Sends a sample alert through every configured channel and reports per-channel HTTP status. Use this to validate webhook setup before relying on alerts. |
| *python main.py check* | Runs 1 batch over *keywords.csv*. Flags: *--keywords path/to.csv* (override default *keywords.csv*), *--concurrency N* (parallel fetches), *--alerts* (send threshold-triggered alerts), *--threshold N* (alert sensitivity in positions, default 3), *--delay 1.0* (seconds between sequential calls). Logs API-call count at end of run. |
| *python main.py schedule --every 86400* | Runs *check* on a fixed interval. Foreground. Use cron / launchd / systemd (see *examples/*) for unattended scheduling. |
| *python main.py report* | Shows the latest snapshot per (keyword × geo × device × locale) with visibility score and Δ vs prior run. Add *--keyword "..."* for a chronological trend, *--location "..."* to filter, or *--html report.html* for a self-contained shareable HTML page. |
| *python main.py competitors --keyword "..."* | **Shows top-N organic results for a keyword + diff vs prior run** (who entered, who left, who moved). Filter with *--location*, *--device*, *--locale*. |
| *python main.py history* | Dumps raw history chronologically. Add *--keyword "..."* to filter. |
| *python main.py export --format csv --output results.csv* | Exports full DB or 1 run (*--run-id N*) to CSV or JSON. |

All commands accept *--db path/to.db* and *--log-level DEBUG*.

## keywords.csv

Required columns: *keyword*, *target_domain*. Optional: *geo*, *locale*, *device_type*, *google_results_language*, *pages*, *target_url*, *active*, *frequency*.

```csv
keyword,target_domain,geo,locale,device_type,google_results_language,pages,active,frequency
best laptop 2026,wired.com,United States,en-us,desktop,en,1,yes,daily
best laptop 2026,wired.com,United Kingdom,en-gb,desktop,en,1,yes,daily
best laptop 2026,wired.com,United States,en-us,mobile,en,1,yes,daily
serp scraping api,decodo.com,United States,en-us,desktop,en,1,yes,weekly
serp scraping api,decodo.com,Germany,de-de,desktop,en,1,yes,weekly
how to scrape google,decodo.com,United Kingdom,en-gb,desktop,en,1,yes,weekly
residential proxies,decodo.com,United States,en-us,desktop,en,1,yes,monthly
old-campaign-keyword,decodo.com,United States,en-us,desktop,en,1,no,daily
```

- *target_domain* matches subdomain-aware (e.g. *example.com* matches *https://blog.example.com/...* but not *https://example.com.evil.com/...*).
- *target_url* (optional) matches the exact URL instead, which is useful for tracking 1 specific blog post.
- *geo* accepts country names, "City,Region,Country", ISO codes, or coordinates. See Decodo's [geolocation docs](https://help.decodo.com/docs/web-scraping-api-google-geolocation).
- *active=no* keeps the historical data but stops new checks, so you can pause a keyword without forgetting it.
- *frequency* is one of *daily* (~22h window), *weekly* (~6.5d), *monthly* (~28d), or *paused*. Each row is skipped if the same (keyword × geo × device × locale) was checked within the window, so you can run the script as often as your cron fires without double-billing.

Each row is 1 check, so to track a keyword across 3 locations you write 3 rows. **Exact duplicates are auto-collapsed at load time** with a warning.

### Cost control

The combination of *active* + *frequency* is your cost lever. On a 50-keyword list with a mix of daily/weekly/monthly tiers, you can expect 30–60% fewer Decodo calls than tracking every keyword daily.

## Visibility score

The visibility score is a single number per result that combines position with SERP-feature ownership, and it appears in the *report* summary. Import *scoring.visibility_score()* to alert on it instead of raw position (run *pip install -e .* from the repo root if importing from outside this directory).

```
score = 100 / √position
      + 30 if featured_snippet_owned
      + 25 / √citation_rank if ai_overview_cited
      + 3 if paa_present
```

Position 1 sets the baseline at 100, and an AI citation at rank 1 adds 25, so a score of *pos 2 + AI-cited #1 + PAA* works out to roughly 98.7.

## Architecture

| File | Role |
|---|---|
| *models.py* | *Pydantic* *RankCheckConfig*, *RankResult*, *AIOverviewCitation*, *CompetitorEntry*. |
| *fetcher.py* | Decodo client with retries, distinct *DecodoCredentialError* / *DecodoAPIError*, and the response parser. |
| *canary.py* | Schema-drift validator. Asserts structural shape (path, types, key presence) without raising a false alarm when content is legitimately absent (e.g. a query that has no PAA block). |
| *storage.py* | SQLite layer with WAL mode + migrations. *runs* + *rank_results* tables. UTC timestamps. Per-call connections, thread-safe. |
| *scheduler.py* | *BatchRunner* (sequential or *ThreadPoolExecutor*), keyword loader, frequency-tier filter, *Alerter* (Slack/Discord/SMTP), heartbeat. |
| *scoring.py* | Composite visibility score. |
| *report.py* | Terminal summary + trend + competitor diff + self-contained HTML report. |
| *main.py* | CLI: *doctor*, *check*, *schedule*, *report*, *competitors*, *export*, *history*, *prune*, *test-alert*. |
| *keywords.csv.example* | Sample input. Copy to *keywords.csv* and edit with your domains. |
| *tests/test_all.py* | 109 unit + integration tests against a captured fixture (no network needed). |
| *examples/* | cron / launchd / systemd templates for unattended runs. |

## Alerting

Set any combination of these in *.env* and add *--alerts* to *check* or *schedule*:

```
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/...   # or Discord
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASSWORD=app-password
ALERT_EMAIL_FROM=you@example.com
ALERT_EMAIL_TO=team@example.com
```

Webhook payload is selected automatically by URL hostname:
- *discord.com/api/webhooks/...* or *discordapp.com/...* → *{"content": "..."}* (Discord)
- Anything else (Slack, generic) → *{"text": "..."}*

Microsoft Teams uses a different webhook protocol and won't accept the simple *{"text": ...}* payload. Since the 2024 deprecation of the legacy O365 Connector, Teams alerts go through Power Automate with Adaptive Card payloads, so treat Teams as a separate integration.

### Heartbeat after every run

When *--alerts* is enabled, a per-run summary is posted to the same channel after each *check* / *schedule* run, even if no thresholds were breached:

```
Rank tracker heartbeat – run #1
• 7 check(s) saved; 7 API call(s) this run (lifetime 7).
• Top movers (≥3 positions):
   - 'best laptop 2026' (United States/desktop): NEW
   - 'best laptop 2026' (United States/mobile): NEW
```

The *NEW* token marks keywords entering the SERP for the first time at that (location, device). Later runs replace it with *prev → current* arrows once there's prior data to compare.

Silence in your Slack now means the tracker stopped, not "everything's fine."

If no channel is configured, alerts are logged to stderr (no silent drops). If a webhook returns 4xx/5xx, the response status and body are logged as a warning.

**Before relying on alerts**, verify delivery:

```bash
python main.py test-alert
# or with a custom message:
python main.py test-alert --message "Hello from rank tracker"
```

Exit codes: 0 = all channels delivered, 1 = one or more failed, 2 = nothing configured.

Alert events:
- *NEW* – keyword entered the SERP for the first time at this (location, device).
- *IMPROVED* / *DROPPED* – position moved by ≥ *--threshold* slots.
- *DROPPED OUT* – a keyword was previously ranked but isn't anymore.

## Running tests

Run the suite to verify your install, or before sending a PR:

```bash
source .venv/bin/activate
python -m unittest tests.test_all -v
```

The suite uses a captured real Decodo response (*tests/fixtures/*) plus *unittest.mock* for transport, so it runs fully offline. Coverage:

- Credential errors and 401 short-circuit
- Retry on 5xx with exponential backoff
- Parser (organic position, AI Overview citation, PAA, exact-URL match, dimensions)
- Domain matching edge cases (substring traps, *www* stripping, subdomains)
- SQLite round-trip
- Alerter branches (Slack / Discord / SMTP, status-code handling)
- CSV loader edge cases
- BatchRunner (sequential, concurrent, one-failure-doesn't-abort)

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | *doctor* or *test-alert* reported at least 1 failure |
| 2 | Required precondition not met (no keyword configs, no alert channel for test-alert, or *prune* missing *--yes*) |
| 3 | Export wrote 0 rows |
| 4 | Decodo rejected credentials |
| 5 | Required file not found |
| 130 | Interrupted (Ctrl+C) |

## Unattended scheduling

The *schedule* command runs in the foreground, so for real automated runs you'll want your OS scheduler. Templates for each platform are included below:

| Platform | Template | Install |
|---|---|---|
| Linux/macOS cron | [examples/crontab.example](examples/crontab.example) | *crontab -e*, paste the line, save |
| macOS launchd | [examples/com.local.rank-tracker.plist](examples/com.local.rank-tracker.plist) | *cp ... ~/Library/LaunchAgents/ && launchctl load ...* |
| Linux systemd (user) | [examples/rank-tracker.service](examples/rank-tracker.service) + [examples/rank-tracker.timer](examples/rank-tracker.timer) | *cp ... ~/.config/systemd/user/ && systemctl --user enable --now rank-tracker.timer* |

After installing, *python main.py doctor* will warn if no scheduled run has completed in 48h (configurable via *--max-staleness-hours*).

## Cost visibility

Each call to Decodo counts, so *BatchRunner* records *api_calls* per run (including retries) and *doctor* prints the lifetime total. After a run you'll see:

```
Run 1 complete – 7 API call(s) this run, 7 lifetime (1 skipped by frequency rules)
```

For 50 keywords across 3 locations on a daily cadence, you can expect roughly 4,500 calls per month.

## Operational hygiene

### Database retention

The DB grows by 1 row per (keyword × geo × device) per run. After a year of daily runs on 50 keywords that's roughly 18k rows, which is well within SQLite's practical limits, but it's still worth pruning periodically:

```bash
# Dry-run (prints what would be deleted, deletes nothing):
python main.py prune --older-than-days 365

# Actually do it (also runs VACUUM to reclaim disk space):
python main.py prune --older-than-days 365 --yes
```

Pair this with a monthly cron entry. The schema uses *ON DELETE CASCADE*, so dropping old *runs* removes their *rank_results* automatically.

### Backups

The whole database is 1 file (*rank_tracker.db*). Back it up with SQLite's online backup:

```bash
sqlite3 rank_tracker.db ".backup '/path/to/backups/rank_tracker-$(date +%F).db'"
```

A weekly cron line is enough, and the file also compresses well.

### Log rotation

If you redirect *python main.py check* output to a log file in cron (*>> rank-tracker.log 2>&1*), use *logrotate* so it doesn't grow unbounded:

```
/path/to/rank-tracker.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

## Scaling beyond SQLite

Migrate to Postgres when you hit any of these conditions: concurrent writes from multiple processes, multiple operators editing keywords at once, or tens of thousands of results per day. Swap *Storage* for a Postgres-backed implementation with the same method surface, since every other module accesses *Storage* only through that interface.

## License

MIT. See [LICENSE](LICENSE).
