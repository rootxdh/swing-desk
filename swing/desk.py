"""Decision desk - one merged verdict per stock.

Combines the swing scanner (trend, setup, size) with today's events (news,
results, reaction) and the open trade from the journal into a single call:
BUY / SHORT / WAIT / MANAGE, with the levels, the reasons, and the warnings.
"""
import yfinance as yf

from swing import engine, events


def decide(res, plan, react, eplan, open_trade=None):
    """Merge scanner plan + event plan + open journal trade into one verdict."""
    reasons, warnings = [], []

    if open_trade is not None:
        side = str(open_trade.get("side") or "long")
        reasons.append("Open " + side + " trade in the journal (id " + str(open_trade.get("id")) +
                       ") - manage it from the fast-actions panel.")
        return {
            "action": "MANAGE", "side": side, "conviction": None,
            "trade_id": int(float(open_trade["id"])),
            "headline": "MANAGE open " + side + " trade",
            "levels": {"trigger": float(open_trade["entry"]), "stop": float(open_trade["stop"]),
                       "target": float(open_trade["target"]), "qty": int(float(open_trade["qty"])),
                       "risk": abs(float(open_trade["entry"]) - float(open_trade["stop"])) * float(open_trade["qty"]),
                       "style": "manage"},
            "source": "journal", "reasons": reasons, "warnings": warnings,
        }

    ev_bias = eplan["bias"] if eplan else "No edge"
    ev_act = bool(eplan and eplan["actionable"])
    ev_long = ev_act and ev_bias.startswith("Long") and eplan["long"]["qty"] >= 1
    ev_short = ev_act and ev_bias.startswith("Short") and eplan["short"]["qty"] >= 1
    ev_nosize = ev_act and not (ev_long or ev_short)
    swing_ok = bool(plan and plan.get("tradeable"))
    qual = plan.get("qual") if plan else None
    setup = plan.get("setup") if plan else None

    if plan:
        if setup:
            reasons.append("Scanner setup: " + setup + " (quality " + str(qual) + ")")
        else:
            reasons.append("Scanner: no clean swing setup at current levels")
    if react:
        reasons.append("Today: " + react["klass"] + " (day " + format(react["chg"], "+.1f") + "%, " +
                       format(react["vol_ratio"], ".1f") + "x volume, closed at " +
                       format(react["close_pos"] * 100, ".0f") + "% of the day's range)")
    if ev_act:
        reasons.append("Event bias: " + ev_bias)
    if res.get("results_flag"):
        reasons.append("Results in play (reported within the last 2 days)")

    def out(action, side, conviction, headline, levels, source):
        return {"action": action, "side": side, "conviction": conviction, "headline": headline,
                "levels": levels, "source": source, "reasons": reasons, "warnings": warnings}

    def swing_levels():
        return {"trigger": plan["entry"], "stop": plan["stop"], "target": plan["target"],
                "qty": plan["qty"], "risk": plan["risk_actual"], "stretch": plan["stretch"],
                "style": "at-close"}

    if swing_ok and ev_short:
        warnings.append("Conflict: the scanner setup is long while today's event reaction is bearish (" +
                        ev_bias + "). Stand aside until the open shows which side is real.")
        return out("WAIT", None, None, "WAIT - conflict: bullish scanner setup vs bearish event reaction",
                   None, None)

    if swing_ok:
        lev = swing_levels()
        if ev_long:
            warnings.append("Scanner and event read both long - still trade only what the levels allow; "
                            "if the open is >1% above entry, do not chase.")
            return out("BUY", "long", "high" if qual == "A" else "medium",
                       "BUY - " + setup + " (quality " + str(qual) + ") + event bias " + ev_bias,
                       lev, "swing")
        return out("BUY", "long", "high" if qual == "A" else "medium",
                   "BUY - " + setup + " (quality " + str(qual) + ")", lev, "swing")

    if ev_long:
        lp = eplan["long"]
        warnings.append("Conditional event trade: only if the trigger is crossed; do not chase "
                        "beyond +1% - wait for a pullback to the trigger or skip.")
        return out("BUY", "long", "high" if "(strong)" in ev_bias else "medium",
                   "BUY above " + format(lp["trigger"], ".2f") + " - event-driven (" + ev_bias + ")",
                   dict(lp, style="breakout"), "event")

    if ev_short:
        sp = eplan["short"]
        warnings.append("Equity shorts are MIS intraday only - square off by 3:20 PM; "
                        "overnight shorts need futures.")
        warnings.append("Conditional event trade: only if the trigger is crossed; do not chase "
                        "beyond -1% - wait for a pullback to the trigger or skip.")
        return out("SHORT", "short", "high" if "(strong)" in ev_bias else "medium",
                   "SHORT below " + format(sp["trigger"], ".2f") + " (MIS intraday) - " + ev_bias,
                   dict(sp, style="breakdown"), "event")

    if ev_nosize:
        warnings.append("Event edge " + ev_bias + " but the position size is 0 at the current capital/risk "
                        "settings - raise the risk budget or skip.")
        return out("WAIT", None, None,
                   "WAIT - " + ev_bias + " event edge, but position size 0 at current capital/risk", None, None)

    if plan and setup:
        return out("WAIT", None, None,
                   "WAIT - " + setup + " setup but quality is " + str(qual) + " (below tradeable)",
                   None, None)
    return out("WAIT", None, None, "WAIT - no clean setup and no significant event", None, None)


def desk_scan(symbols, capital=engine.DEFAULT_SETTINGS["capital"],
              risk_pct=engine.DEFAULT_SETTINGS["risk_pct"], period="2y",
              hours=40, refresh=False, progress=None):
    """One pass per symbol: swing scanner + events + merged decision."""
    nifty = None
    try:
        nifty, _, _ = engine.fetch("NIFTY", period, refresh)
    except Exception:
        pass

    opens = {}
    try:
        jdf = engine.load_journal()
        if len(jdf):
            for _, row in jdf[jdf["status"] == "open"].iterrows():
                opens[str(row["symbol"]).strip().upper()] = row.to_dict()
    except Exception:
        pass

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
            rs = engine.rel_strength(d, nifty) if nifty is not None else None
            if rs is not None:
                res["rs63"] = rs
            plan = engine.swing_plan(res, d, capital, risk_pct)

            react = events.reaction(d)
            ticker = yf.Ticker(ym)
            news = events.stock_news(ticker, hours=hours)
            earn = events.earnings_info(ticker)
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

            eplan = events.event_plan(react, res, capital, risk_pct) if react else None
            decision = decide(res, plan, react, eplan, open_trade=opens.get(sym.strip().upper()))
            out.append({"symbol": res["symbol"], "metrics": res, "plan": plan, "react": react,
                        "eplan": eplan, "news": news, "earn": earn, "decision": decision,
                        "event_flags": "; ".join(res_flags) or "-", "error": None})
        except Exception as e:
            out.append({"symbol": sym.strip().upper(), "error": str(e)})
    if progress:
        progress(1.0, "done")
    return out
