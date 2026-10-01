"""Shared engine for the CLI (analyze.py) and the Swing Desk platform (app.py).

Everything here is read-only market analysis; no orders are ever placed by this module.
"""
import datetime as dt
import json
import os

import numpy as np
import pandas as pd
import yfinance as yf

from swing import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
REPORTS_DIR = os.path.join(ROOT, "reports")
CHARTS_DIR = os.path.join(REPORTS_DIR, "charts")
JOURNAL_PATH = os.path.join(ROOT, "journal", "trades.csv")
SETTINGS_PATH = os.path.join(ROOT, "settings.json")

ALIASES = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "BANKNIFTY50": "^NSEBANK",
    "SENSEX": "^BSESN",
}

DEFAULT_SETTINGS = {"capital": 100000.0, "risk_pct": 1.0,
                    "trail_gate_r": 1.0, "trail_giveback": 30.0}


def to_yf_symbol(sym):
    s = sym.strip().upper()
    if s in ALIASES:
        return ALIASES[s]
    if s.startswith("^") or "." in s:
        return s
    return s + ".NS"


def display_name(ym):
    return ym.lstrip("^").replace(".NS", "").replace(".BO", "")


def fetch(symbol, period="2y", refresh=False):
    ym = to_yf_symbol(symbol)
    fname = ym.lstrip("^").replace(".", "_") + "_" + period + ".csv"
    path = os.path.join(DATA_DIR, fname)
    if os.path.exists(path) and not refresh:
        age_h = (dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600
        if age_h < 24:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            df = df.dropna(subset=["Close"])
            if len(df):
                return df, ym, True
    df = yf.download(ym, period=period, interval="1d", auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None, ym, False
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        df.to_csv(path)
    except OSError:
        pass  # the CSV is only a cache; read-only hosts just refetch each time
    return df, ym, False


def add_indicators(df):
    d = df.copy()
    c = d["Close"]
    for n in (20, 50, 200):
        d["SMA" + str(n)] = c.rolling(n).mean()

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["RSI14"] = 100 - 100 / (1 + rs)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    d["MACD"] = ema12 - ema26
    d["MACD_SIG"] = d["MACD"].ewm(span=9, adjust=False).mean()
    d["MACD_HIST"] = d["MACD"] - d["MACD_SIG"]

    h, l, pc = d["High"], d["Low"], c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["ATR14"] = tr.ewm(alpha=1 / 14, min_periods=14).mean()

    d["VOL20"] = d["Volume"].rolling(20).mean()
    d["VOL50"] = d["Volume"].rolling(50).mean()
    return d


def label(frac):
    if frac >= 0.6:
        return "Uptrend"
    if frac >= 0.2:
        return "Mildly bullish"
    if frac > -0.2:
        return "Neutral / mixed"
    if frac > -0.6:
        return "Mildly bearish"
    return "Downtrend"


def metrics(symbol, df, ym):
    """Trend metrics for one stock. Returns (result_dict, indicator_df)."""
    d = add_indicators(df)
    last = d.iloc[-1]
    close = float(last["Close"])

    def ret(days):
        if len(d) > days:
            base = float(d["Close"].iloc[-1 - days])
            if base > 0:
                return (close / base - 1) * 100
        return None

    def cagr(years):
        n = int(252 * years)
        if len(d) > n:
            base = float(d["Close"].iloc[-1 - n])
            if base > 0:
                return ((close / base) ** (1 / years) - 1) * 100
        return None

    def val(col):
        v = last.get(col)
        return float(v) if v is not None and pd.notna(v) else None

    sma20, sma50, sma200 = val("SMA20"), val("SMA50"), val("SMA200")
    rsi, macd, macd_sig, hist = val("RSI14"), val("MACD"), val("MACD_SIG"), val("MACD_HIST")
    atr = val("ATR14")
    vol20, vol50 = val("VOL20"), val("VOL50")

    win = d.tail(252)
    hi52, lo52 = float(win["High"].max()), float(win["Low"].min())
    pos52 = (close - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None

    sup3 = float(d["Low"].tail(63).min())
    res3 = float(d["High"].tail(63).max())

    slope200 = None
    if len(d) > 21 and pd.notna(d["SMA200"].iloc[-21]) and d["SMA200"].iloc[-21] > 0:
        slope200 = (float(last["SMA200"]) / float(d["SMA200"].iloc[-21]) - 1) * 100

    golden = death = None
    diff = d["SMA50"] - d["SMA200"]
    prev = diff.shift()
    up_idx = d.index[(diff > 0) & (prev <= 0)]
    dn_idx = d.index[(diff < 0) & (prev >= 0)]
    if len(up_idx):
        golden = up_idx[-1].date().isoformat()
    if len(dn_idx):
        death = dn_idx[-1].date().isoformat()

    st_checks = []
    if sma20 is not None:
        st_checks.append(("Close above 20-DMA", close > sma20))
    if sma20 is not None and sma50 is not None:
        st_checks.append(("20-DMA above 50-DMA", sma20 > sma50))
    if rsi is not None:
        st_checks.append(("RSI(14) above 50", rsi > 50))
    if hist is not None:
        st_checks.append(("MACD histogram positive", hist > 0))
    if macd is not None:
        st_checks.append(("MACD above zero line", macd > 0))
    st_score = sum(1 if ok else -1 for _, ok in st_checks)
    st_frac = st_score / len(st_checks) if st_checks else 0.0

    lt_checks = []
    if sma200 is not None:
        lt_checks.append(("Close above 200-DMA", close > sma200))
    if slope200 is not None:
        lt_checks.append(("200-DMA rising (1 month)", slope200 > 0))
    if sma50 is not None and sma200 is not None:
        lt_checks.append(("50-DMA above 200-DMA (golden state)", sma50 > sma200))
    c = cagr(3) if cagr(3) is not None else cagr(1)
    if c is not None:
        lt_checks.append(("Positive 1-3y CAGR", c > 0))
    if pos52 is not None:
        if pos52 >= 60:
            lt_checks.append(("Upper half of 52-week range", True))
        elif pos52 <= 40:
            lt_checks.append(("Upper half of 52-week range", False))
    lt_score = sum(1 if ok else -1 for _, ok in lt_checks)
    lt_frac = lt_score / len(lt_checks) if lt_checks else 0.0

    notes = []
    if rsi is not None:
        if rsi >= 70:
            notes.append("RSI overbought (>=70): momentum strong but stretched")
        elif rsi <= 30:
            notes.append("RSI oversold (<=30): weak momentum, possible reversal zone")
    if pos52 is not None:
        if pos52 >= 98:
            notes.append("At / breaking to 52-week high")
        elif pos52 <= 2:
            notes.append("At / breaking to 52-week low")
    if golden:
        notes.append("Golden cross (50-DMA crossed above 200-DMA) on " + golden)
    if death:
        notes.append("Death cross (50-DMA crossed below 200-DMA) on " + death)
    if slope200 is not None:
        notes.append("200-DMA slope (last month): " + format(slope200, "+.2f") + "%")
    if vol20 is not None and vol50 is not None and vol50 > 0:
        notes.append("Volume: 20-day avg is " + format(vol20 / vol50 - 1, "+.0%") + " vs 50-day avg")
    if atr is not None and close > 0:
        notes.append("Daily volatility (ATR14): " + format(atr / close * 100, ".2f") + "% of price")

    res = {
        "symbol": display_name(ym),
        "name": symbol.upper(),
        "close": close,
        "date": str(last.name.date()) if hasattr(last.name, "date") else str(last.name),
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "rsi": rsi, "macd": macd, "macd_sig": macd_sig, "hist": hist, "atr": atr,
        "vol20": vol20, "vol50": vol50,
        "r1w": ret(5), "r1m": ret(21), "r3m": ret(63), "r6m": ret(126), "r1y": ret(252),
        "cagr3": cagr(3), "cagr5": cagr(5),
        "hi52": hi52, "lo52": lo52, "pos52": pos52,
        "sup3": sup3, "res3": res3,
        "st_label": label(st_frac), "st_score": st_score, "st_n": len(st_checks),
        "lt_label": label(lt_frac), "lt_score": lt_score, "lt_n": len(lt_checks),
        "slope200": slope200,
        "notes": notes,
    }
    return res, d


def weekly_uptrend(d):
    try:
        w = d["Close"].resample("W-FRI").last().dropna()
        if len(w) < 35:
            return None
        sma30 = w.rolling(30).mean()
        if pd.isna(sma30.iloc[-1]):
            return None
        return bool(w.iloc[-1] > sma30.iloc[-1])
    except Exception:
        return None


def rel_strength(d, nifty_df, days=63):
    if nifty_df is None or len(d) <= days or len(nifty_df) <= days:
        return None
    s = float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-1 - days]) - 1
    n = float(nifty_df["Close"].iloc[-1]) / float(nifty_df["Close"].iloc[-1 - days]) - 1
    return (s - n) * 100


def swing_plan(res, d, capital=DEFAULT_SETTINGS["capital"], risk_pct=DEFAULT_SETTINGS["risk_pct"]):
    """Classify a swing setup and build entry/stop/target/size. Long-only, delivery style.

    Brackets: stop = 1.5 x ATR below entry (floored at 2%, capped at 8% of price),
    target = 2R, stretch = 52-week high or 3R.
    """
    close = res["close"]
    atr = res["atr"] or close * 0.02
    sma20, sma50, sma200 = res["sma20"], res["sma50"], res["sma200"]
    rsi, res3, hi52 = res["rsi"], res["res3"], res["hi52"]
    wu = weekly_uptrend(d)

    setup = None
    notes = []
    if sma20 and sma50 and sma200:
        off_high = (res3 - close) / res3 if res3 else 0
        near_3m_high = close >= 0.985 * res3
        if sma20 > sma50 > sma200 and close > sma20 and rsi is not None and rsi >= 54 and near_3m_high:
            setup = "Breakout / momentum"
            notes.append("At 3-month high with stacked rising averages (20>50>200)")
            if close >= 0.98 * hi52:
                notes.append("Also at/near 52-week high")
        elif close > sma200 and sma50 > sma200 and off_high >= 0.03 and rsi is not None and 38 <= rsi <= 58 \
                and abs(close - sma20) / sma20 <= 0.02:
            setup = "Pullback in uptrend"
            notes.append("Pullback to 20-DMA inside a 50>200 uptrend; buy-the-dip zone")
        elif close > sma20 > sma50 > sma200 and rsi is not None and 52 <= rsi <= 72:
            setup = "Trend continuation"
            notes.append("Orderly uptrend (price>20>50>200); momentum still constructive")
        elif rsi is not None and rsi <= 32 and close > sma200:
            setup = "Oversold bounce (counter-trend)"
            notes.append("RSI oversold with 200-DMA intact: bounce play, smaller size, quick exit")

    stop_dist = min(max(1.5 * atr, 0.02 * close), 0.08 * close)
    entry = close
    stop = entry - stop_dist
    target = entry + 2 * stop_dist
    stretch = max(hi52, entry + 3 * stop_dist)
    rr = (target - entry) / stop_dist if stop_dist else None

    risk_amt = capital * risk_pct / 100
    qty = int(risk_amt / stop_dist) if stop_dist > 0 else 0
    qty = min(qty, int(capital * 0.25 / entry)) if entry > 0 else 0
    qty = min(qty, int(capital / entry)) if entry > 0 else 0
    if qty < 1:
        notes.append("Position size 0: risk budget too small for this stop distance - skip or raise capital/risk")
    value = qty * entry
    risk_actual = qty * stop_dist

    q = 0
    if sma200 and close > sma200:
        q += 2
    else:
        notes.append("Below 200-DMA: long trades work against the primary trend")
    if sma50 and sma200 and sma50 > sma200:
        q += 1
    if sma20 and sma50 and sma20 > sma50:
        q += 1
    if wu:
        q += 1
    if setup in ("Breakout / momentum", "Pullback in uptrend"):
        q += 2
    elif setup == "Trend continuation":
        q += 1
    if res.get("rs63") is not None and res["rs63"] > 0:
        q += 1
    vol20, vol50 = res.get("vol20"), res.get("vol50")
    if vol20 and vol50 and vol20 > vol50:
        q += 1
    if rsi is not None and rsi > 75:
        q -= 1
        notes.append("RSI stretched (>75): late entry risk")
    if setup == "Oversold bounce (counter-trend)":
        q -= 1
    q = max(q, 0)
    if q >= 8:
        qual = "A"
    elif q >= 6:
        qual = "B"
    elif q >= 4:
        qual = "C"
    else:
        qual = "weak"

    tradeable = setup is not None and q >= 4 and qty >= 1
    return {
        "setup": setup,
        "quality": q,
        "qual": qual,
        "tradeable": tradeable,
        "entry": entry, "stop": stop, "target": target, "stretch": stretch,
        "stop_dist": stop_dist, "rr": rr,
        "qty": qty, "value": value, "risk_actual": risk_actual,
        "weekly_uptrend": wu,
        "notes": notes,
    }


def screen(symbols, capital=DEFAULT_SETTINGS["capital"], risk_pct=DEFAULT_SETTINGS["risk_pct"],
           period="2y", refresh=False, progress=None):
    nifty = None
    try:
        nifty, _, _ = fetch("NIFTY", period, refresh)
    except Exception:
        pass
    out = []
    n = max(len(symbols), 1)
    for i, sym in enumerate(symbols):
        if progress:
            progress(i / n, sym)
        try:
            df, ym, _ = fetch(sym, period, refresh)
            if df is None:
                out.append({"symbol": sym.strip().upper(), "metrics": None, "plan": None, "error": "no data"})
                continue
            res, d = metrics(sym, df, ym)
            res["rs63"] = rel_strength(d, nifty)
            plan = swing_plan(res, d, capital, risk_pct)
            out.append({"symbol": res["symbol"], "metrics": res, "plan": plan, "error": None})
        except Exception as e:
            out.append({"symbol": sym.strip().upper(), "metrics": None, "plan": None, "error": str(e)})
    if progress:
        progress(1.0, "done")
    return out


JOURNAL_COLS = store.TRADE_COLS


def load_journal():
    if store.have_db():
        df = store.load_trades_df()
    elif os.path.exists(JOURNAL_PATH):
        try:
            df = pd.read_csv(JOURNAL_PATH)
        except Exception:
            df = pd.DataFrame(columns=JOURNAL_COLS)
    else:
        df = pd.DataFrame(columns=JOURNAL_COLS)
    for c in JOURNAL_COLS:
        if c not in df.columns:
            df[c] = None
    df["side"] = df["side"].fillna("long")
    return df[JOURNAL_COLS]


def save_journal(df):
    if store.have_db():
        store.save_trades_df(df)
        return
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    df.to_csv(JOURNAL_PATH, index=False)


def add_trade(symbol, qty, entry, stop, target, setup="", quality="", notes="", date_in=None, side="long"):
    with store.lock:
        df = load_journal()
        tid = int(df["id"].max() + 1) if len(df) and pd.notna(df["id"].max()) else 1
        row = {
            "id": tid, "status": "open", "date_in": date_in or dt.date.today().isoformat(),
            "symbol": symbol, "side": side, "qty": qty, "entry": entry, "stop": stop, "target": target,
            "setup": setup, "quality": quality, "notes": notes,
            "date_out": None, "exit_price": None, "pnl": None, "r_multiple": None,
        }
        new = pd.DataFrame([row], columns=JOURNAL_COLS)
        df = pd.concat([df, new], ignore_index=True) if len(df) else new
        save_journal(df)
        return tid


def update_trade(trade_id, stop=None, target=None):
    with store.lock:
        df = load_journal()
        mask = df["id"] == trade_id
        if not mask.any():
            return False
        if stop is not None:
            df.loc[mask, "stop"] = float(stop)
        if target is not None:
            df.loc[mask, "target"] = float(target)
        save_journal(df)
        return True


def close_trade(trade_id, exit_price, date_out=None):
    with store.lock:
        df = load_journal()
        mask = df["id"] == trade_id
        if not mask.any():
            return False
        row = df.loc[mask].iloc[0]
        entry, stop, qty = float(row["entry"]), float(row["stop"]), float(row["qty"])
        side = str(row.get("side") or "long").lower()
        if side == "short":
            pnl = (entry - exit_price) * qty
            r = (entry - exit_price) / (stop - entry) if stop != entry else 0.0
        else:
            pnl = (exit_price - entry) * qty
            r = (exit_price - entry) / (entry - stop) if entry != stop else 0.0
        df.loc[mask, "status"] = "closed"
        df["date_out"] = df["date_out"].astype(object)
        df.loc[mask, "date_out"] = date_out or dt.date.today().isoformat()
        df.loc[mask, "exit_price"] = exit_price
        df.loc[mask, "pnl"] = round(pnl, 2)
        df.loc[mask, "r_multiple"] = round(r, 2)
        save_journal(df)
        return True


def trail_plan(side, entry, initial_stop, extreme, qty, giveback_pct=30.0, gate_r=1.0):
    """Giveback trail for an open trade: the stop follows the best price and gives
    back giveback_pct of the run since entry, but only after the trade banked gate_r
    x risk (1R = entry - initial stop). Ratchets only - never loosens the stop - and
    mirrors for shorts. Pure math, no I/O; extreme = best price reached since entry."""
    side = (side or "long").lower()
    long_side = side != "short"
    entry = float(entry)
    extreme = float(extreme)
    qty = int(qty or 0)
    gb = max(0.0, float(giveback_pct)) / 100.0
    gate = max(0.0, float(gate_r))
    stop0 = float(initial_stop) if initial_stop is not None else None

    per_share = (extreme - entry) if long_side else (entry - extreme)
    risk = None
    if stop0 is not None:
        risk = (entry - stop0) if long_side else (stop0 - entry)

    out = {"side": side, "entry": entry, "extreme": extreme, "qty": qty,
           "per_share_best": per_share, "stop_now": stop0, "gate": gate,
           "r_best": None, "active": False, "raw_stop": None, "suggested_stop": None,
           "improves": False, "lock_per_share": None, "lock_rs": None, "lock_pct": None,
           "reason": ""}

    if risk is not None and risk > 0:
        out["r_best"] = per_share / risk
        armed = out["r_best"] >= gate
        need_txt = ("+" + format(gate, ".2f") + "R = Rs " + format(gate * risk, ",.2f") +
                    "/share needed, best so far " + format(out["r_best"], "+.2f") + "R")
    else:
        # Journal stop already trailed to/beyond entry - R is no longer computable
        # from the journal alone, so any open profit keeps the trail armed.
        armed = per_share > 0
        need_txt = "stop already at/beyond entry - any open profit keeps the trail armed"

    if per_share <= 0:
        out["reason"] = ("Trail not armed: trade has not traded above entry yet (best "
                         + format(per_share, "+,.2f") + "/share). Gate: " + need_txt + ".")
        return out

    raw = (entry + per_share * (1.0 - gb)) if long_side else (entry - per_share * (1.0 - gb))
    out["raw_stop"] = round(raw, 2)
    if not armed:
        out["reason"] = ("Trail not armed: " + need_txt + ". At this peak the trail would "
                         "suggest Rs " + format(raw, ",.2f") + ".")
        return out

    sug = round(raw, 2)
    if stop0 is not None:
        sug = (max(raw, stop0) if long_side else min(raw, stop0))
        sug = round(sug, 2)
    out["active"] = True
    out["suggested_stop"] = sug
    out["improves"] = (stop0 is None) or (sug > stop0 + 1e-9 if long_side else sug < stop0 - 1e-9)
    out["lock_per_share"] = round((sug - entry) if long_side else (entry - sug), 2)
    out["lock_pct"] = round(out["lock_per_share"] / entry * 100.0, 2)
    out["lock_rs"] = round(out["lock_per_share"] * qty, 2)
    if out["improves"]:
        out["reason"] = ("Trail armed: peak Rs " + format(extreme, ",.2f") + " (+" +
                         format(per_share, ",.2f") + "/share). Suggest stop Rs " +
                         format(sug, ",.2f") + " - locks Rs " + format(out["lock_rs"], "+,.0f") +
                         " (" + format(out["lock_pct"], "+.1f") + "% vs entry).")
    else:
        out["reason"] = ("Trail armed: peak Rs " + format(extreme, ",.2f") + ". Current stop Rs " +
                         format(stop0, ",.2f") + " is already at or better than the trail suggests.")
    return out


def load_settings():
    if store.have_db():
        s = dict(DEFAULT_SETTINGS)
        s.update(store.kv_get("settings", {}) or {})
        return s
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            s = dict(DEFAULT_SETTINGS)
            s.update(data)
            return s
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(data):
    if store.have_db():
        store.kv_set("settings", data)
        return
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def read_watchlist(path):
    symbols = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    symbols.append(ln)
    return symbols
