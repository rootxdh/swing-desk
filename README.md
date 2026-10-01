# Stocks-qoder — NSE swing desk + trend analysis workspace

Personal workspace for Zerodha / NSE analysis. Learning material: [Zerodha Varsity](https://zerodha.com/varsity/modules/).
Everything (scripts, data, reports) lives inside this folder.

## What is set up

| Piece | Purpose |
|---|---|
| `app.py` — **Swing Desk** (Streamlit) | The assisted-trading platform: decision desk (scanner + events merged into one call per stock), IPO desk (listing-day plays), live exit guidance, journal |
| `swing/engine.py` | Shared engine: data fetch (Yahoo, 24h cache), indicators, swing plans, screening, journal |
| `swing/events.py` | Event features: today's news + results, reaction quality, next-day long/short plans |
| `swing/desk.py` | Decision engine: merges swing plan + event plan + open journal trade into one call (MANAGE / BUY / SHORT / WAIT) |
| `swing/kite_mcp.py` | Live Kite feed + order passthrough via Zerodha's official Kite MCP server (quotes, daily candles, place/modify orders, GTTs). One Zerodha-hosted login per session — the app never sees your password |
| `swing/orders.py` | Order register for orders placed from the app: status/fill sync (fills auto-journal, exits close trades), GTT helpers, day-aware exit spec |
| `swing/store.py` | Storage layer: local files by default; Neon Postgres when `DATABASE_URL` is set (trades table + app_state JSONB). `python -m swing.store status\|migrate` |
| `swing/ipo.py` | IPO desk engine: live GMP board + subscriptions (investorgain.com, 20-min cache), listing history stats, scoring, listing-day ticket with ratcheting trail stop |
| `analyze.py` | CLI: fetch NSE data, trend signals, markdown report + PNG charts |
| `zerodha/connect.py` | Kite Connect helper: login, daily token, read-only portfolio snapshot |
| `data/` | Cached daily OHLCV CSVs (24h cache; "Fresh prices" checkbox to force) + `ipo_cache/` (20-min) |
| `reports/` | `trend_report_<date>.md` + `charts/<SYMBOL>.png` |
| `watchlists/` | Symbol lists, one per line (`default.txt` = 20 Nifty heavyweights) |
| `journal/` | Trade journal (`trades.csv`) — entry/exit logged from the Decision desk (or My Trades) |

Packages installed via `pip --user` (numpy<2, pandas, matplotlib, yfinance, streamlit, plotly, kiteconnect, jinja2≥3.1.2).
Note: `python -m venv` is broken on this machine — use the global `python`.

## Run the Swing Desk

```bash
python -m streamlit run app.py
```

Then open http://localhost:8501. Four tabs:

1. **Decision desk (scan + events)** — one *Run full scan* computes everything per stock and reduces it
   to a single call: the swing setup (quality grade, entry, ATR stop, 2R target, size for your
   capital/risk %) **merged with** today's news/results reaction and your open journal trades →
   **MANAGE** (you already hold a trade), **BUY**, **SHORT** (event breakdown, MIS intraday) or
   **WAIT** (with the reason: weak quality, scanner-vs-event conflict, or position size 0).
   "Today's calls" lists what needs attention and the combined table colours every call.
   Pick a stock and everything about it is in one panel: levels, **profit at target and loss at stop
   in both ₹ and %**, why-this-call bullets + warnings, news, results, chart — plus the
   **fast actions**: *LOG ENTRY* (buys or shorts exactly as recommended) and *LOG EXIT* for an open
   trade. Copy-ready Kite order tickets (swing CNC; event long/short) sit in expanders below.
2. **IPO desk (listing play)** — see the dedicated section below.
3. **My Trades (fast out)** — open positions with live prices, current R multiple, distance to
   stop, and a plain action ("Hold", "Move stop to breakeven", "Near stop", "Target hit",
   "EXIT NOW"). Shorts are tracked with inverted maths. **Giveback trail**: once a trade is up
   *+1R* (sidebar → Risk settings), the manage panel suggests the ratcheting stop — best price
   since entry **minus 30% of the run** (both adjustable), never loosening. *Apply* updates the
   journal and, with the live feed connected, moves the Kite GTT stop leg in the same click.
4. **Journal** — win rate, total P&L, open risk, average R, full trade log (CSV download).

## IPO desk (listing-day play)

One button pulls the live grey-market boards (investorgain.com JSON API, cached 20 min):
the GMP board (upcoming/open/closed/listed issues), live subscription figures and a
~250-listing performance tracker. The desk scores each closed issue (GMP %, subscription,
issue size, QIB, analyst rating) and builds a **ticket for listing day** only when the odds
are historically good — GMP ≥ 10% (below that the empirical edge disappears).

**Empirical rule behind it** (last ~250 listings): GMP ≥ 20% → 98% listed positive, median
+43.6%; GMP 10–20% → 91% positive, median +16.7%. Buy-at-listing → hold-to-close drift is
~+6% median, with a worst case of −26.6% — hence the mandatory stop.

**Exit = ratcheting trail stop.** You hold while it makes new highs; the stop gives back at
most the *giveback %* (default 30%) of the **maximum profit since entry**:

```
stop = peak − 30% × (peak − entry)          e.g. entry ₹100 → peak ₹120 → stop ₹114 (locks ₹14)
                                             peak ₹130 → stop ₹121 (locks ₹21)
```

The stop only ever moves **up** (never down). Until the price clears the entry, the initial
hard stop (−5% mainboard, −7% SME) holds. The tab shows the worked ladder (peak → stop →
locked ₹) and a *listing-day trail check*: type the highest price seen since entry and it
tells you the new stop to set. **Kite does not trail stops automatically** — after each new
high you raise the SL-M / GTT trigger yourself. SME issues trade in 5% circuits with
lot-size minimums; the ticket flags it when one lot exceeds your risk budget. GMP is
unofficial: odds, not guarantees. Data source: investorgain.com live boards.

## Zerodha account connection (optional, read-only)

The app never sees your Zerodha password or TOTP — you log in on Zerodha's own page and only a
one-time `request_token` is exchanged. Kite's free personal tier covers portfolio reads; live
prices stay on Yahoo (Kite market data costs ₹500/month).

1. Create a Kite Connect app at https://developers.kite.trade (free) → note API key + secret.
2. Copy `zerodha/.env.example` to `zerodha/.env` and paste the key/secret.
3. One-time per day (token expires ~7:30 AM IST):
   ```bash
   python zerodha/connect.py login          # prints a login URL
   python zerodha/connect.py token "<full redirect URL after login>"
   ```
4. `python zerodha/connect.py status` to check, `python zerodha/connect.py snapshot` for a
   markdown portfolio report in `reports/`. The app's sidebar and My Trades tab pick the
   session up automatically.

Secrets stay in `zerodha/.env` and `zerodha/token.json` — both git-ignored. Orders placed from
the app are **confirm-gated** (explicit checkbox + button per action) and their writes **never
auto-retry** (no duplicate-order risk); the copy-ready ticket flow still works side by side.

The sidebar **Kite live feed + order actions** is a second route: the app acts as its own MCP
client to Zerodha's hosted **Kite MCP server** (https://mcp.kite.trade). One Zerodha-hosted
authorize link per app session (still no password/TOTP to the app); after that, live LTPs and
daily candles appear across the desk, My Trades and IPO tabs, and the order/GTT buttons become
active. The app's own MCP session is independent of any chat-side integration.

## Data & storage

Everything works out of the box with local files. Setting `DATABASE_URL` (e.g. a free Neon
Postgres) in `zerodha/.env` switches the journal, settings, order register and Kite session to
the database — one shared source of truth for both the local app and any hosted copy:

```bash
python -m swing.store status     # backend, row counts, state keys
python -m swing.store migrate    # one-shot: local files → database (last write wins)
```

## Deploy to Streamlit Community Cloud (free)

The app is deploy-ready: storage lives in Neon Postgres, so the hosted copy and your local copy
share one journal, and a password gate protects the public URL.

1. The code is on GitHub (`swing-desk`, private by default).
2. On https://share.streamlit.io sign in with GitHub → **New app** → pick the `swing-desk`
   repo, main file `app.py`, Python **3.12** (or 3.11 — `numpy<2` has wheels up to 3.12).
3. **Advanced settings → Secrets**, paste (values from your `zerodha/.env` / choice):

   ```toml
   DATABASE_URL = "postgresql://...your Neon connection string..."
   SWING_PASSWORD = "choose-a-password"
   ```

4. Deploy. The gate asks for the password once per browser session (only active when
   `SWING_PASSWORD` is set — local runs without it stay open).

Limits to remember: the free tier **sleeps after inactivity** (wakes on next visit, ~30 s),
and an always-on order watcher would need a host that never sleeps — order placement stays
confirm-gated in the UI either way. Render's free tier works the same way if you prefer it.

Note on live data: real-time prices need the paid Kite Connect data tier (₹500/month) — the free
tier has quotes/orders but no market data. Zerodha also runs an official **Kite MCP server**
(https://mcp.kite.trade) for AI assistants; either route still needs the same Kite Connect app.

## CLI analysis (no UI)

```bash
python analyze.py RELIANCE TCS INFY                 # analyse specific stocks
python analyze.py --watchlist watchlists/default.txt # scan a watchlist
python analyze.py RELIANCE --fundamentals            # add P/E, ROE, margins, growth, div yield
python analyze.py NIFTY BANKNIFTY --period 10y       # indexes too; longer history for long-term CAGR
```

## Signals used (and why)

**Short-term (days–weeks, Varsity Technical Analysis):**
close vs 20/50-DMA, 20-DMA vs 50-DMA, RSI(14) vs 50, MACD histogram, MACD vs zero line → score → Uptrend / Mildly bullish / Neutral / Mildly bearish / Downtrend.

**Long-term (months–years, Varsity Fundamental Analysis + trend):**
close vs 200-DMA, 200-DMA slope (1 month), 50-DMA vs 200-DMA (golden/death cross state), 1–3y CAGR, position in 52-week range.

**Swing plan:** stop = 1.5×ATR (clamped 2–8% of price), target = 2R, stretch = max(52-week high, 3R); position size = risk% × capital ÷ stop distance, capped at 25% of capital; quality grade from setup + trend + volume + relative strength vs Nifty.

**Event plan:** today's reaction (gap, day %, close position in range, volume ratio, range vs ATR) → bias score (± swing with 200-DMA and gap) → tomorrow's long trigger = today's high + 0.08%, short trigger = today's low − 0.08%, stops 1.5×ATR from trigger, targets 2R.

## How to work with the assistant

1. **Ask for a scan** — "analyse these 10 stocks" → I run the scanner, then interpret the outputs (structure, levels, momentum, risks) in plain language.
2. **Ask for deep-dives** — one stock: multi-timeframe (daily/weekly), support/resistance zones, fundamental overlay, what would change the read (invalidation levels).
3. **Event days** — "results/news came today, what's tomorrow's plan?" → I run the Decision desk scan, walk you through the verdicts and tickets for the stocks that matter.
4. **Risk discipline (Varsity Module 9)** — position sizing from your capital/risk %, stop-loss placement from ATR/support, R:R before entry; trades logged in `journal/`.
5. **Portfolio review** — connect Kite (or paste holdings) and I flag trend breaks, concentration, relative strength.

## Honest limits

- Rule-based signals from Yahoo Finance EOD/delayed data. Verify prices on Kite before acting.
- A news day does **not** predict tomorrow's direction. The event plans are conditional levels — the market confirms which side is real; both tickets are pre-planned so you never improvise at the open.
- **Equity shorting on NSE is MIS intraday only — square off by 3:20 PM; carry shorts overnight only via futures.** Short tickets are labelled accordingly.
- This is **educational/analytical support, not SEBI-registered investment advice**. Final decisions and risk are yours.
- LLM + indicators can be wrong; treat output as a first pass, not a verdict.
