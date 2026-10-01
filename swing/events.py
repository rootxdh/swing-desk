"""Event-driven next-day plans.

For each stock: collect today's news and earnings/results info, measure how the
stock reacted today (gap, close position in range, volume vs normal), and turn
that into conditional plans for tomorrow's open (long breakout / short breakdown).

Honest framing: the future move cannot be predicted. What this module produces is
a reaction-quality read plus pre-computed levels, so direction is decided by the
market at open - the trade triggers only if the level is crossed.
"""
import datetime as dt

import pandas as pd
import yfinance as yf

from swing import engine

POS_WORDS = ["beat", "beats", "profit up", "profit rises", "record", "order win", "wins order",
             "bags order", "buyback", "bonus", "dividend", "upgrade", "surges", "jumps", "soars",
             "rally", "growth", "expansion", "approval", "approves", "deal", "acquisition",
             "partnership", "extends gains", "hits high"]
NEG_WORDS = ["miss", "misses", "profit falls", "profit down", "profit drops", "loss", "probe",
             "penalty", "fine", "resign", "downgrade", "falls", "slumps", "declines", "tumbles",
             "plunges", "fraud", "default", "recall", "lawsuit", "raid", "cuts guidance",
             "weak", "layoff", "stake sale", "block deal"]


def _sentiment(title):
    t = (title or "").lower()
    p = sum(1 for w in POS_WORDS if w in t)
    n = sum(1 for w in NEG_WORDS if w in t)
    if p > n:
        return "positive"
    if n > p:
        return "negative"
    return "neutral"


def _parse_news_item(it):
    title = it.get("title")
    ts = publisher = link = None
    content = it.get("content")
    if isinstance(content, dict):
        title = content.get("title") or title
        pub = content.get("pubDate") or content.get("displayTime")
        ts = pd.to_datetime(pub, utc=True, errors="coerce") if pub else None
        prov = content.get("provider")
        publisher = prov.get("displayName") if isinstance(prov, dict) else None
        for key in ("canonicalUrl", "clickThroughUrl"):
            u = content.get(key)
            if isinstance(u, dict) and u.get("url"):
                link = u["url"]
                break
    else:
        if it.get("providerPublishTime"):
            ts = pd.to_datetime(it["providerPublishTime"], unit="s", utc=True)
        publisher = it.get("publisher")
        link = it.get("link")
    return {"title": title, "publisher": publisher, "time": ts, "link": link}


def stock_news(ticker, hours=40, max_items=12):
    try:
        items = ticker.news or []
    except Exception:
        return []
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)
    out = []
    for it in items[:max_items]:
        if not isinstance(it, dict):
            continue
        n = _parse_news_item(it)
        if not n.get("title"):
            continue
        if n.get("time") is not None:
            if n["time"] < cutoff:
                continue
            n["time_ist"] = n["time"].tz_convert("Asia/Kolkata")
        else:
            n["time_ist"] = None
        n["sentiment"] = _sentiment(n["title"])
        out.append(n)
    return out


def earnings_info(ticker):
    info = {"next_date": None, "last_date": None, "eps_actual": None, "eps_est": None,
            "surprise": None, "recent": False, "results_today": None}
    today = dt.date.today()
    try:
        cal = ticker.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            ds = [d.date() if hasattr(d, "date") else d for d in dates]
            ds = [d for d in ds if isinstance(d, dt.date)]
            fut = sorted(d for d in ds if d >= today)
            past = sorted(d for d in ds if d < today)
            if fut:
                info["next_date"] = fut[0].isoformat()
            if past:
                info["last_date"] = past[-1].isoformat()
            if any(d == today for d in ds):
                info["results_today"] = today.isoformat()
    except Exception:
        pass
    try:
        ed = ticker.get_earnings_dates(limit=8)
        if ed is not None and len(ed):
            reported = ed[ed["Reported EPS"].notna()] if "Reported EPS" in ed.columns else ed.iloc[0:0]
            if len(reported):
                last_dt = reported.index.max()
                last_day = last_dt.date() if hasattr(last_dt, "date") else last_dt
                row = reported.loc[last_dt]
                info["eps_actual"] = None if pd.isna(row.get("Reported EPS")) else float(row["Reported EPS"])
                info["eps_est"] = None if pd.isna(row.get("EPS Estimate")) else float(row["EPS Estimate"])
                info["surprise"] = None if pd.isna(row.get("Surprise(%)")) else float(row["Surprise(%)"])
                if isinstance(last_day, dt.date):
                    info["last_date"] = last_day.isoformat()
                    if last_day == today or (today - last_day).days <= 1:
                        info["results_today"] = last_day.isoformat()
    except Exception:
        pass
    if info["last_date"]:
        try:
            ld = dt.date.fromisoformat(info["last_date"])
            info["recent"] = (today - ld).days <= 2
        except ValueError:
            pass
    return info


def reaction(d):
    """Today's candle vs prior context: gap, change, close position, volume ratio."""
    if len(d) < 60:
        return None
    t, p = d.iloc[-1], d.iloc[-2]
    atr = float(d["ATR14"].iloc[-2]) if pd.notna(d["ATR14"].iloc[-2]) else float(t["Close"]) * 0.02
    vol50 = float(d["VOL50"].iloc[-2]) if pd.notna(d["VOL50"].iloc[-2]) else float(t["Volume"])
    o, h, l, c = float(t["Open"]), float(t["High"]), float(t["Low"]), float(t["Close"])
    pc = float(p["Close"])
    rng = h - l
    gap = (o / pc - 1) * 100 if pc else 0
    chg = (c / pc - 1) * 100 if pc else 0
    close_pos = (c - l) / rng if rng > 0 else 0.5
    vol_ratio = float(t["Volume"]) / vol50 if vol50 else 1.0
    rng_atr = rng / atr if atr else 1.0
    significant = vol_ratio >= 1.5 and (abs(chg) >= 1.0 or rng_atr >= 1.2)
    if chg >= 1 and close_pos >= 0.6 and vol_ratio >= 1.2:
        klass = "Bullish reaction"
    elif chg <= -1 and close_pos <= 0.4 and vol_ratio >= 1.2:
        klass = "Bearish reaction"
    elif chg >= 0.3 and close_pos >= 0.55:
        klass = "Mildly positive"
    elif chg <= -0.3 and close_pos <= 0.45:
        klass = "Mildly negative"
    else:
        klass = "Mixed / indecisive"
    return {
        "date": str(t.name.date()) if hasattr(t.name, "date") else str(t.name),
        "open": o, "high": h, "low": l, "close": c, "prev_close": pc,
        "gap": gap, "chg": chg, "close_pos": close_pos, "vol_ratio": vol_ratio,
        "rng": rng, "rng_atr": rng_atr, "atr": atr,
        "significant": bool(significant), "klass": klass,
    }


def event_plan(react, res, capital=engine.DEFAULT_SETTINGS["capital"],
               risk_pct=engine.DEFAULT_SETTINGS["risk_pct"]):
    """Conditional plans for the next session, both sides. Direction is confirmed by the market."""
    close, h, l, atr = react["close"], react["high"], react["low"], react["atr"]
    chg, cp, volx, gap = react["chg"], react["close_pos"], react["vol_ratio"], react["gap"]
    sma200 = res.get("sma200")

    score = 0
    if chg >= 1 and cp >= 0.6:
        score += 2
    elif chg >= 0.3 and cp >= 0.55:
        score += 1
    elif chg <= -1 and cp <= 0.4:
        score -= 2
    elif chg <= -0.3 and cp <= 0.45:
        score -= 1
    if volx >= 1.5:
        score += 1 if chg > 0 else (-1 if chg < 0 else 0)
    if sma200:
        score += 1 if close > sma200 else -1
    if gap > 0.3 and chg > 0:
        score += 1
    if gap < -0.3 and chg < 0:
        score -= 1
    if score >= 3:
        bias = "Long (strong)"
    elif score >= 1:
        bias = "Long"
    elif score == 0:
        bias = "No edge"
    elif score >= -2:
        bias = "Short"
    else:
        bias = "Short (strong)"

    buf = max(0.0008 * close, 0.05)
    risk_amt = capital * risk_pct / 100

    lt = h + buf
    ls = max(l - buf, lt - 1.5 * atr)
    lq = int(risk_amt / (lt - ls)) if lt > ls else 0
    lq = min(lq, int(capital * 0.25 / lt)) if lt > 0 else 0
    long_plan = {"trigger": lt, "stop": ls, "target": lt + 2 * (lt - ls), "qty": lq,
                 "risk": lq * (lt - ls), "value": lq * lt}

    st_trigger = l - buf
    ss = min(h + buf, st_trigger + 1.5 * atr)
    sq = int(risk_amt / (ss - st_trigger)) if ss > st_trigger else 0
    sq = min(sq, int(capital * 0.25 / st_trigger)) if st_trigger > 0 else 0
    short_plan = {"trigger": st_trigger, "stop": ss, "target": st_trigger - 2 * (ss - st_trigger),
                  "qty": sq, "risk": sq * (ss - st_trigger), "value": sq * st_trigger}

    actionable = (react["significant"] or res.get("results_flag")) and bias != "No edge"
    rule = ("If the open is already beyond the trigger by more than ~1%, do not chase - "
            "wait for a pullback to the trigger level, or skip the trade.")
    return {
        "bias": bias, "score": score, "actionable": bool(actionable),
        "long": long_plan, "short": short_plan, "rule": rule,
        "levels": {"today_high": h, "today_low": l, "today_close": close},
    }


def event_scan(symbols, capital=engine.DEFAULT_SETTINGS["capital"],
               risk_pct=engine.DEFAULT_SETTINGS["risk_pct"], period="2y",
               hours=40, refresh=False, progress=None):
    out = []
    n = max(len(symbols), 1)
    for i, sym in enumerate(symbols):
        if progress:
            progress(i / n, sym)
        try:
            df, ym, _ = engine.fetch(sym, period, refresh)
            if df is None:
                out.append({"symbol": sym.strip().upper(), "error": "no data"})
                continue
            res, d = engine.metrics(sym, df, ym)
            react = reaction(d)
            ticker = yf.Ticker(ym)
            news = stock_news(ticker, hours=hours)
            earn = earnings_info(ticker)
            res_flags = []
            if earn.get("results_today"):
                res_flags.append("results " + str(earn["results_today"]))
                res["results_flag"] = True
            elif earn.get("recent"):
                res_flags.append("results " + str(earn.get("last_date")))
                res["results_flag"] = True
            if earn.get("next_date"):
                res_flags.append("next results " + str(earn["next_date"]))
            if news:
                res_flags.append(str(len(news)) + " news")
            if react and react["significant"]:
                res_flags.append("big move day")
            plan = event_plan(react, res, capital, risk_pct) if react else None
            out.append({"symbol": res["symbol"], "metrics": res, "react": react, "plan": plan,
                        "news": news, "earn": earn, "event_flags": "; ".join(res_flags) or "-",
                        "error": None})
        except Exception as e:
            out.append({"symbol": sym.strip().upper(), "error": str(e)})
    if progress:
        progress(1.0, "done")
    return out
