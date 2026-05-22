# polymarket-copytrader

Polymarket "smart money" signal generator. Fetches the top-N traders by month-PnL,
parallel-fetches their open positions, and scores each market by rank-weighted,
portfolio-normalized consensus. Flags markets where smart money disagrees with the
current market price ("edge candidates"). Sends rich Discord embeds. Optional
Kalshi cross-venue matching for US-legal betting.

This is a **research tool**. It does not place real orders.

## Why not just bet on Polymarket?

Polymarket is geoblocked from the US, UK, France, Singapore, and other jurisdictions
(CFTC settlement). If you're in a non-blocked country, the signal generator's output
maps directly to Polymarket orders. From a US IP, you can still:

- Use the signal to inform bets on **Kalshi** (CFTC-regulated, US-legal — politics,
  econ, some sports). The `kalshi-match` command finds the most similar open Kalshi
  market for each Polymarket edge candidate.
- Use it as research / market color.
- Compare to US sportsbook lines (DraftKings, FanDuel) for sports markets — top
  Polymarket traders are mostly sports bettors and their consensus often disagrees
  with sportsbook public lines.

## Setup

```bash
cd polymarket-copytrader
python -m pip install -e .[dev]
cp .env.example .env
# edit .env: set DISCORD_WEBHOOK_URL
```

Make a Discord webhook: server settings -> Integrations -> Webhooks -> New Webhook -> Copy URL.

## Commands

```bash
copytrader show                          # one-shot: fetch + score + print, no DB write, no Discord
copytrader snapshot                      # fetch + score + persist to SQLite
copytrader snapshot --notify             # ...and send a Discord digest
copytrader snapshot --notify --no-write  # send digest without persisting
copytrader resolve                       # check pending paper trades; compute realized P&L
copytrader kalshi-match                  # for top edge candidates, find Kalshi equivalent (best-effort)
copytrader init-db                       # create the SQLite schema (idempotent)
```

### Kalshi matching caveat
Kalshi's market titles are concatenated outcome lists (e.g. `yes James Harden: 1+, yes OG Anunoby...`)
rather than human-readable game names. Fuzzy title matching against Polymarket titles like
`"Toronto Blue Jays vs. New York Yankees"` will frequently return `(no match)` for sports
markets. The matcher works best for cleanly-named politics/econ/weather markets. Sports
cross-venue matching probably needs a per-game/per-event index, not title fuzzing.

## How the score works

```
score(market) = sum over top-N traders of [
    rank_weight(trader_rank)
    * (trader_position_value / trader_total_open_portfolio)
    * direction         # +1 for YES, -1 for NO
]
```

- `rank_weight(r) = 1 / sqrt(r)` — top-ranked traders weigh more, but lower-ranked
  traders still contribute.
- Position size is normalized by each trader's *own* portfolio, so a $50k bet from
  a $200k account is weighted the same as a $5M bet from a $20M account.
- A market is flagged as an **edge candidate** if smart-money consensus disagrees
  with the current Polymarket YES price by more than `EDGE_THRESHOLD` (default 0.08).

## Filters (configurable via .env)

- `MIN_TRADERS_PER_MARKET` — minimum top traders in a market before scoring (default 4)
- `MIN_CONSENSUS_SCORE` — minimum |score| (default 0.15)
- `MIN_MARKET_VOLUME_USD` — skip illiquid markets (default $10k)
- `MIN_MINUTES_TO_RESOLUTION` — skip markets resolving in < N minutes (default 60)
- `EDGE_THRESHOLD` — how much smart money has to disagree with the market price (default 0.08)

## Storage

SQLite at `data/copytrader.db` (configurable). Schema is append-only — every
`snapshot` run inserts a new keyed row, building forward-only history that the
paper-trade backtest replays once markets resolve.

Tables: `snapshots`, `leaderboard`, `positions`, `market_scores`, `paper_trades`.

## Running it daily

### Hosted (GitHub Actions — recommended)
A daily workflow at [.github/workflows/daily.yml](.github/workflows/daily.yml) runs
`snapshot --notify` + `resolve` + `export-csv` every day at 14:00 UTC and commits
the updated CSV / DB back to the repo. To enable it:

1. **Generate a long-lived Claude auth token locally** (so auto-research works in CI
   using your Pro/Max subscription, no API charges):
   ```
   claude setup-token
   ```
   This opens a browser for OAuth, then prints a long token starting with `sk-ant-oat...`.
   Copy it.

2. Push this repo to GitHub.

3. Add two repository secrets (Settings → Secrets and variables → Actions → New repository secret):
   - `DISCORD_WEBHOOK_URL` — your Discord webhook
   - `CLAUDE_CODE_OAUTH_TOKEN` — the token from step 1

4. The first run will fire at the next 14:00 UTC, or you can trigger it manually
   from the Actions tab (workflow_dispatch).

The workflow installs `@anthropic-ai/claude-code` via npm and reads
`CLAUDE_CODE_OAUTH_TOKEN` from the secret, so headless `claude -p` calls in
research.py authenticate against your subscription with no per-call billing.

You can revoke the token at any time from your Anthropic account settings, and
regenerate a fresh one with `claude setup-token` if it leaks.

### Other hosting options
- **Hetzner / DigitalOcean / Lightsail VPS (~$4-5/mo)**: install Python, clone
  the repo, cron a daily run. Auto-research works if you install + log into
  `claude` once on the box.
- **Windows Task Scheduler / cron on a laptop**: only fires when the laptop is on.
- **Fly.io / Railway / Render scheduled jobs**: similar to GitHub Actions but
  paid.

For your week-long monitoring experiment, GitHub Actions is the right answer —
zero infra cost, runs whether your PC is on or off, and the spreadsheet stays
synced to the repo.

## Weekly tracking spreadsheet

`copytrader export-csv` dumps every detected market (across every snapshot)
into [data/predictions.csv](data/predictions.csv) with columns:

- `snapshot_ts`, `market_title`, `end_date`, `condition_id`
- `consensus_side`, `smart_money_dollars`, `n_traders`
- `market_yes_price`, `consensus_score`, `edge`, `was_edge_candidate`
- `suggested_bet_side`, `suggested_bet_usd`
- `resolved_outcome`, `we_predicted_correctly`, `realized_pnl`, `resolved_at`
- `polymarket_url`

Open in Excel / Google Sheets / Numbers — that's your weekly journal. The
GitHub Actions workflow regenerates this file every day automatically.

## Backtest

The retrospective backtest path (walk current winners backward through their
trade history) is **survivorship-biased and disabled by design** — it conditions
on people we already know won, so the reported edge is fake.

Instead, every `snapshot --paper` run logs paper trades for that snapshot's edge
candidates into the `paper_trades` table at the current orderbook fill price.
When the underlying market resolves, the `paper-pnl` job (TODO) computes
realized P&L net of Polymarket's 2% taker fee. Honest forward-only data.

## Repo layout

```
src/copytrader/
  config.py        env-driven config
  models.py        pydantic models for API responses + MarketScore internal type
  polymarket.py    Data API + Gamma API async client (retry/backoff)
  signals.py       consensus scoring + filtering
  storage.py       SQLite persistence (append-only snapshots)
  notifier.py      Discord webhook embeds
  kalshi.py        Kalshi read-only client + fuzzy title matcher
  cli.py           typer CLI

tests/             pytest + respx (HTTP mocking)
data/              SQLite DB + parquet exports (gitignored)
```

## Status

- [x] Phase 0 validation script (`validate_signal.py`, ad-hoc)
- [x] Polymarket API client + pydantic models
- [x] Consensus scorer + filters (unique-trader counting, pinned-price filter)
- [x] SQLite storage (append-only snapshot history)
- [x] Discord notifier (rich embeds for digests and edge alerts)
- [x] Kalshi fuzzy title matcher (limited — see caveat above)
- [x] Typer CLI (`show`, `snapshot`, `resolve`, `kalshi-match`, `init-db`)
- [x] Paper-trade resolution job + cumulative P&L summary
- [x] Tests (13 passing): signals math, storage roundtrip, paper-trade math
- [ ] Long-running scheduler (APScheduler) for real-time sports alerts
- [ ] Smarter Kalshi matcher (per-event index, team-name extraction)
- [ ] Send a test Discord message (requires user-provided webhook URL)
