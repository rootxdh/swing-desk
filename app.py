"""Swing Desk - local NSE swing-trading platform (assisted, not automated).

Run:  streamlit run app.py     (then open http://localhost:8501)

Fast in:  Decision desk merges scanner + events into one verdict per stock,
          with a ready-to-place order ticket and fast-action journal buttons.
Fast out: My Trades monitors stops/targets and tells you what to do.
"""
import datetime as dt
import hmac
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from swing import desk, engine, events, ipo, kite_mcp, orders

# Streamlit Cloud secrets -> environment, so swing/* (which never imports
# streamlit) reads DATABASE_URL and the app password like plain env vars.
try:
    for _k in ("DATABASE_URL", "SWING_PASSWORD"):
        if _k not in os.environ and _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

st.set_page_config(page_title="Swing Desk", layout="wide")

# Password gate for public deployments: active only when SWING_PASSWORD is set
# (host secrets / env). Local runs without it stay open. Session-scoped: each
# new browser session asks once.
_gate_pw = os.environ.get("SWING_PASSWORD") or ""
if _gate_pw and not st.session_state.get("auth_ok"):
    st.title("Swing Desk")
    st.caption("Private workspace - enter the password to continue.")
    _try_pw = st.text_input("Password", type="password", key="_pw_try")
    if _try_pw and hmac.compare_digest(_try_pw, _gate_pw):
        st.session_state["auth_ok"] = True
        st.rerun()
    if _try_pw:
        st.error("Wrong password.")
    st.stop()

WATCHLIST_DIR = os.path.join(ROOT, "watchlists")


def kite_client():
    try:
        from kiteconnect import KiteConnect
        from zerodha import connect as zc
    except ImportError:
        return None, "kiteconnect not installed"
    env = zc.load_env()
    tok = zc.load_token()
    if not env.get("KITE_API_KEY") or not tok:
        return None, "not connected - see README 'Zerodha account connection'"
    k = KiteConnect(api_key=env["KITE_API_KEY"])
    k.set_access_token(tok["access_token"])
    return k, "session " + str(tok.get("user_id")) + " saved " + str(tok.get("date"))


@st.cache_resource(show_spinner=False)
def kite_mcp_client():
    km_ = kite_mcp.KiteMCP()
    try:
        km_.start()
    except Exception:
        pass
    km_.start_keepalive()
    return km_


@st.cache_data(ttl=15, show_spinner=False)
def kite_state():
    try:
        return kite_mcp_client().status()
    except Exception as e:
        return "error", str(e)


@st.cache_data(ttl=12, show_spinner=False)
def live_ltp(symbols):
    """{symbol: last_price} from the live Kite feed; {} when it is not connected."""
    try:
        if kite_state()[0] != "live":
            return {}
        return {s: p for s, p in kite_mcp_client().ltp(list(symbols)).items() if p is not None}
    except Exception:
        return {}


@st.cache_data(ttl=12, show_spinner=False)
def live_quote(symbol):
    """Full live quote dict for one symbol (last_price, ohlc, volume) or None."""
    try:
        if kite_state()[0] != "live":
            return None
        return kite_mcp_client().quotes([symbol]).get(str(symbol).strip())
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def trade_extreme(side, symbol, date_in):
    """Best price since entry: (extreme, source/reason). Live Kite daily candles when
    the feed is authorised, else Yahoo daily bars (delayed). Long trades track the
    high, shorts the low; the live day high/low is folded in. On failure the second
    element explains why instead of failing silently."""
    long_side = str(side).lower() != "short"
    try:
        start = dt.date.fromisoformat(str(date_in)[:10])
    except Exception:
        start = dt.date.today()
    err = None
    try:
        if kite_state()[0] == "live":
            days = max(30, (dt.date.today() - start).days + 10)
            rows = [c for c in kite_mcp_client().historical_daily(symbol, days=days)
                    if c.get("date") >= start.isoformat()]
            if rows:
                ext = max(c["high"] for c in rows) if long_side else min(c["low"] for c in rows)
                oh = ((live_quote(symbol) or {}).get("ohlc") or {})
                if long_side and oh.get("high"):
                    ext = max(ext, float(oh["high"]))
                if not long_side and oh.get("low"):
                    ext = min(ext, float(oh["low"]))
                return float(ext), "kite"
            err = "Kite candles returned no bars since " + start.isoformat()
    except Exception as e:
        err = "Kite candles failed: " + str(e)[:140]
    try:
        dfx, _, _ = engine.fetch(symbol, "6mo")
        if dfx is None or not len(dfx):
            err = err or "no Yahoo price history for " + str(symbol)
        else:
            idx = dfx.index
            ts = pd.Timestamp(start, tz="UTC") if getattr(idx, "tz", None) is not None else pd.Timestamp(start)
            d = dfx[idx >= ts]
            if len(d):
                return (float(d["High"].max()) if long_side else float(d["Low"].min())), "yahoo"
            err = "no Yahoo bars since " + start.isoformat()
    except Exception as e:
        err = "Yahoo fetch failed: " + str(e)[:140]
    return None, (err or "no price data")


def resolve_symbols(choice, custom):
    if custom.strip():
        raw = [s.strip().upper() for s in custom.replace(",", "\n").splitlines()]
    else:
        raw = engine.read_watchlist(os.path.join(WATCHLIST_DIR, choice))
    seen, out = set(), []
    for s in raw:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fmt(v, dp=2):
    return "-" if v is None else format(v, ",." + str(dp) + "f")


def make_detail_chart(d, plan, months=8):
    w = d.tail(21 * months)
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=w.index, open=w["Open"], high=w["High"], low=w["Low"], close=w["Close"], name="Price",
        increasing_line_color="#16a34a", decreasing_line_color="#dc2626",
    ))
    for col, color in (("SMA20", "#f59e0b"), ("SMA50", "#2563eb"), ("SMA200", "#7c3aed")):
        if col in w:
            fig.add_trace(go.Scatter(x=w.index, y=w[col], name=col, line=dict(width=1.2, color=color)))
    if plan and plan.get("entry"):
        for y, color, lab in ((plan["entry"], "#16a34a", "Entry"), (plan.get("stop"), "#dc2626", "Stop"),
                              (plan.get("target"), "#2563eb", "Target")):
            if y is not None:
                fig.add_hline(y=y, line_dash="dash", line_color=color, annotation_text=lab, annotation_position="right")
    fig.update_layout(height=450, xaxis_rangeslider_visible=False, margin=dict(l=10, r=60, t=20, b=10),
                      legend=dict(orientation="h", y=1.02))
    return fig


def order_ticket(sym, plan, capital, risk_pct):
    return (
        "SYMBOL      : " + sym + "  (NSE)\n"
        "SIDE        : BUY\n"
        "PRODUCT     : CNC (delivery)\n"
        "ORDER TYPE  : LIMIT\n"
        "PRICE       : " + format(plan["entry"], ".2f") + "   (do NOT chase above " + format(plan["entry"] * 1.01, ".2f") + ")\n"
        "QUANTITY    : " + str(plan["qty"]) + "\n"
        "STOP-LOSS   : SELL SL-M, trigger " + format(plan["stop"], ".2f") + "\n"
        "TARGET      : " + format(plan["target"], ".2f") + "   (stretch " + format(plan["stretch"], ".2f") + ")\n"
        "IF TARGET   : +Rs " + format((plan["target"] - plan["entry"]) * plan["qty"], ",.0f") + " (+" +
        format((plan["target"] / plan["entry"] - 1) * 100, ".1f") + "%)\n"
        "IF STOP     : -Rs " + format((plan["entry"] - plan["stop"]) * plan["qty"], ",.0f") + " (-" +
        format((1 - plan["stop"] / plan["entry"]) * 100, ".1f") + "%)\n"
        "RISK        : Rs " + format(plan["risk_actual"], ",.0f") + " of Rs " + format(capital, ",.0f") +
        " (" + format(risk_pct, ".2f") + "%)\n"
        "VALUE       : about Rs " + format(plan["value"], ",.0f") + "\n"
        "SETUP       : " + str(plan.get("setup")) + " | quality " + str(plan.get("qual"))
    )


def event_ticket(sym, side, pl):
    if side == "long":
        head = ("BUY (long breakout)", "CNC (delivery)", "above " + format(pl["trigger"], ".2f"),
                "no chase if the open is > trigger+1%; wait for pullback to trigger")
    else:
        head = ("SELL (short breakdown) - MIS INTRADAY ONLY",
                "MIS (square off by 3:20 PM; overnight shorts need futures)",
                "below " + format(pl["trigger"], ".2f"),
                "no chase if the open is < trigger-1%; wait for pullback to trigger")
    if side == "long":
        prof, prof_pct = (pl["target"] - pl["trigger"]) * pl["qty"], (pl["target"] / pl["trigger"] - 1) * 100
        loss, loss_pct = (pl["trigger"] - pl["stop"]) * pl["qty"], (1 - pl["stop"] / pl["trigger"]) * 100
    else:
        prof, prof_pct = (pl["trigger"] - pl["target"]) * pl["qty"], (1 - pl["target"] / pl["trigger"]) * 100
        loss, loss_pct = (pl["stop"] - pl["trigger"]) * pl["qty"], (pl["stop"] / pl["trigger"] - 1) * 100
    lines = [
        "SYMBOL      : " + sym + "  (NSE)",
        "SIDE        : " + head[0],
        "PRODUCT     : " + head[1],
        "ORDER TYPE  : SL-M / stop-limit",
        "TRIGGER     : " + head[2],
        "STOP-LOSS   : " + format(pl["stop"], ".2f"),
        "TARGET      : " + format(pl["target"], ".2f"),
        "IF TARGET   : +Rs " + format(prof, ",.0f") + " (+" + format(prof_pct, ".1f") + "%)",
        "IF STOP     : -Rs " + format(loss, ",.0f") + " (-" + format(loss_pct, ".1f") + "%)",
        "QUANTITY    : " + str(pl["qty"]),
        "RISK        : Rs " + format(pl["risk"], ",.0f"),
        "RULE        : " + head[3],
    ]
    return "\n".join(lines)


def ipo_ticket(r, plan):
    cat = "SME" if plan["sme"] else "MAINBOARD"
    gb = plan.get("giveback_pct", 30.0)
    lines = [
        "SYMBOL      : " + r["name"] + "  (" + cat + ", NSE) - lists " + str(r["listing_dt"]),
        "SIDE        : BUY on listing day (CNC delivery)",
        "GMP IMPLIED : Rs " + fmt(plan["exp_listing"]) + "  (issue Rs " + fmt(r["price"]) + " + GMP Rs " + fmt(r["gmp_rs"]) + ")",
        "BUY ZONE    : Rs " + fmt(plan["zone_lo"]) + " - " + fmt(plan["zone_hi"]),
        "ENTRY REF   : Rs " + fmt(plan["entry"]),
    ]
    if plan["sme"]:
        lines.append("LOTS        : " + str(plan["qty"] // max(int(plan["lot"]), 1)) + " lot(s) of " +
                     str(int(plan["lot"])) + " shares")
    lines += [
        "QUANTITY    : " + str(plan["qty"]),
        "HARD STOP   : Rs " + fmt(plan["stop"]) + "  (-" + format(plan["stop_pct"], ".0f") +
        "%)  holds until the price clears entry",
        "TRAIL STOP  : stop = peak - " + format(gb, ".0f") + "% of max profit (peak - entry); only moves UP",
    ]
    for x in plan.get("trail") or []:
        if x["peak_pct"] in (5, 10, 20):
            lines.append("  peak Rs " + fmt(x["peak"]) + " (+" + format(x["peak_pct"], ".0f") + "%) -> stop Rs " +
                         fmt(x["stop"]) + "  (locks +Rs " + format(x["locked_rs"], ",.0f") + ")")
    lines += [
        "WORST CASE  : -Rs " + format(plan["risk_rs"], ",.0f") + " (-" + format(plan["stop_pct"], ".0f") +
        "%) if it never clears entry; after that the trail keeps you in profit",
        "VALUE       : about Rs " + format(plan["value"], ",.0f"),
        "RULES       : buy only inside the zone; if it opens above Rs " + fmt(plan["no_chase_above"]) +
        " do NOT chase - wait for a pullback to the zone or skip.",
        "              if it opens below Rs " + fmt(plan["skip_below"]) + " the GMP story failed - skip.",
        "              hold while it makes new highs; after each new high raise the SL-M/GTT trigger to the",
        "              trail stop. Exit when the trail stop is hit. Never move the stop down.",
    ]
    return "\n".join(lines)


def ipo_bucket_line(gmp_pct, buckets):
    """The historic outcome for the bucket this IPO's GMP falls into."""
    if gmp_pct is None:
        return None
    if gmp_pct >= 20:
        key = "GMP >= 20%"
    elif gmp_pct >= 10:
        key = "GMP 10-20%"
    elif gmp_pct >= 5:
        key = "GMP 5-10%"
    elif gmp_pct >= 1:
        key = "GMP 1-5%"
    else:
        key = "GMP <= 1%"
    b = next((x for x in buckets if x["bucket"] == key), None)
    if not b:
        return None
    line = ("History for " + key + " (n=" + str(b["n"]) + "): listing " +
            format(b["listing_median"], "+.1f") + "% median, " + format(b["listing_pos_pct"], ".0f") + "% positive")
    if b["drift_median"] is not None:
        line += (" | buy-at-listing -> close: " + format(b["drift_median"], "+.1f") + "% median, " +
                 format(b["drift_pos_pct"], ".0f") + "% positive, worst " + format(b["drift_worst"], "+.1f") + "%")
    return line


def ipo_scatter(perf):
    fig = go.Figure()
    for cat, color in (("IPO", "#2563eb"), ("SME", "#f59e0b")):
        pts = [r for r in perf if r["cat"] == cat and r["gmp_pct"] is not None and r["listing_gain"] is not None]
        fig.add_trace(go.Scatter(
            x=[p["gmp_pct"] for p in pts], y=[p["listing_gain"] for p in pts], mode="markers",
            name=cat, marker=dict(size=7, color=color, opacity=0.65),
            text=[p["name"] + " (" + str(p["listing_dt"]) + ")" for p in pts],
            hovertemplate="%{text}<br>GMP %{x:.1f}% -> listing %{y:.1f}%<extra></extra>",
        ))
    top = max([r["listing_gain"] for r in perf if r["listing_gain"] is not None] or [10])
    fig.add_trace(go.Scatter(x=[-10, top], y=[-10, top], mode="lines", name="GMP = outcome",
                             line=dict(color="#94a3b8", dash="dash", width=1)))
    fig.add_hline(y=0, line_color="#cbd5e1", line_width=1)
    fig.update_layout(height=380, xaxis_title="GMP % at listing", yaxis_title="Actual listing gain %",
                      margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.05))
    return fig


def level_strip(dec):
    lev = dec.get("levels") or {}
    if not lev:
        return None
    bits = []
    if lev.get("trigger") is not None:
        bits.append("Trigger Rs " + fmt(lev["trigger"]))
    if lev.get("stop") is not None:
        bits.append("Stop Rs " + fmt(lev["stop"]))
    if lev.get("target") is not None:
        bits.append("Target Rs " + fmt(lev["target"]))
    if lev.get("qty"):
        bits.append("Qty " + str(int(lev["qty"])))
    if lev.get("risk") is not None:
        bits.append("Risk Rs " + format(lev["risk"], ",.0f"))
    side = dec.get("side")
    if side and all(lev.get(k) is not None for k in ("trigger", "stop", "target", "qty")):
        prof, upct, loss, lpct = reward_read(side, float(lev["trigger"]), float(lev["stop"]),
                                             float(lev["target"]), int(lev["qty"]))
        bits.append("At target +Rs " + format(prof, ",.0f") + " (+" + format(upct, ".1f") +
                    "%) / at stop -Rs " + format(loss, ",.0f") + " (-" + format(lpct, ".1f") + "%)")
    pref = {"at-close": "Swing entry (CNC, at CMP / next open): ",
            "breakout": "Buy ONLY above trigger: ",
            "breakdown": "Short ONLY below trigger (MIS intraday): ",
            "manage": "Open trade levels: "}.get(lev.get("style"), "")
    return pref + " | ".join(bits)


def exit_read(side, entry, stop, target, ltp):
    short = str(side).lower() == "short"
    if entry == stop:
        return "no price", None, None
    r_now = (entry - ltp) / (stop - entry) if short else (ltp - entry) / (entry - stop)
    dist = (stop - ltp) / ltp * 100 if short else (ltp - stop) / ltp * 100
    if short:
        if ltp >= stop:
            return "EXIT NOW - above stop", r_now, dist
        if ltp <= target:
            return "Target hit - book or trail", r_now, dist
    else:
        if ltp <= stop:
            return "EXIT NOW - below stop", r_now, dist
        if ltp >= target:
            return "Target hit - book or trail", r_now, dist
    if r_now >= 1:
        return "Move stop to breakeven", r_now, dist
    if dist <= 1.5:
        return "Near stop - watch closely", r_now, dist
    return "Hold", r_now, dist


def reward_read(side, entry, stop, target, qty):
    """(profit Rs, profit %, loss Rs, loss %) at target / stop, measured from entry."""
    long = str(side).lower() != "short"
    if long:
        return ((target - entry) * qty, (target / entry - 1) * 100,
                (entry - stop) * qty, (1 - stop / entry) * 100)
    return ((entry - target) * qty, (1 - target / entry) * 100,
            (stop - entry) * qty, (stop / entry - 1) * 100)


def place_entry_form(symbol, dec, lev, p, pp, rx, lstate, key):
    """Confirm-gated real entry order from the calculated plan (Kite MCP)."""
    if lstate != "live":
        st.info("Connect the live feed (sidebar) to place orders from the app. "
                "Until then, place the ticket above in Kite yourself.")
        return
    km_ = kite_mcp_client()
    long_side = (dec.get("side") or "long") == "long"
    is_event = dec.get("source") == "event"
    trig = float(lev["trigger"])
    mkt = kite_mcp.market_open_now()
    options = ["SL-M (conditional)", "LIMIT", "MARKET (now)"]
    default = 0 if is_event else (2 if mkt else 1)
    st.caption("SL-M = stop order: sits untriggered and fires only if price crosses the trigger - the tool for "
               "tomorrow's open. MARKET only works while the market is open.")
    otype = st.radio("Order type", options, index=default, key=key + "_ot", label_visibility="collapsed")
    c1, c2 = st.columns(2)
    qty = c1.number_input("Quantity", min_value=1, value=max(int(lev["qty"]), 1), key=key + "_q")
    if otype.startswith("SL-M"):
        price_arg = None
        trig_arg = round(c2.number_input("Trigger price (Rs)", min_value=0.01, value=round(trig, 2),
                                         step=0.05, key=key + "_t"), 2)
    elif otype == "LIMIT":
        trig_arg = None
        price_arg = round(c2.number_input("Limit price (Rs)", min_value=0.01, value=round(trig, 2),
                                          step=0.05, key=key + "_p"), 2)
    else:
        price_arg = trig_arg = None
    kite_type = {"SL-M (conditional)": "SL-M", "LIMIT": "LIMIT", "MARKET (now)": "MARKET"}[otype]
    product = "CNC" if long_side else "MIS"
    tt = "BUY" if long_side else "SELL"
    st.code(tt + " " + symbol + " x" + str(int(qty)) + " | " + kite_type + " | " + product +
            ((" | trigger Rs " + fmt(trig_arg)) if trig_arg else "") +
            ((" | limit Rs " + fmt(price_arg)) if price_arg else "") +
            " | plan stop Rs " + fmt(lev.get("stop")) + " | plan target Rs " + fmt(lev.get("target")) +
            " | NSE", language=None)
    if not long_side:
        st.caption("MIS short = intraday only, square off by 3:20 PM. Place the stop (SL-M BUY) and target "
                   "(LIMIT BUY) from My Trades > Manage after it fills.")
    else:
        st.caption("After it fills: My Trades > Manage places a protective GTT (stop + target) on Kite in one step.")
    ok = st.checkbox("I understand this places a real order in my Zerodha account", key=key + "_ok")
    if st.button("PLACE ORDER", type="primary", disabled=not ok, use_container_width=True, key=key + "_btn"):
        try:
            res = km_.place_order(tradingsymbol=symbol, transaction_type=tt, quantity=int(qty),
                                  product=product, order_type=kite_type, price=price_arg,
                                  trigger_price=trig_arg, tag="SQD-ENTRY")
            oid = res.get("order_id") if isinstance(res, dict) else None
            if oid:
                orders.record("entry", oid, symbol=symbol, side=dec.get("side") or "long", qty=int(qty),
                              product=product, stop=float(lev.get("stop")), target=float(lev.get("target")),
                              planned_entry=(trig_arg or price_arg),
                              setup=(rx["klass"] if (is_event and rx) else (p.get("setup") if p else "")),
                              quality=(pp["bias"] if (is_event and pp) else (p.get("qual") if p else "")),
                              source=("event" if is_event else "swing"))
            st.success("Order placed" + ((": " + str(oid)) if oid else "" ) +
                       " - track it in My Trades > 'Orders placed from the app' and click Check after it fills.")
        except Exception as e:
            st.error("Order failed: " + str(e))


def exit_now_block(tid, sym, side, qty, ltp=None, lstate="", key=""):
    """Checkbox-confirmed real early exit: cancel live protections, place the exit
    order, and let the next sync close the journal trade at the actual fill."""
    if lstate != "live":
        st.info("Connect the live feed (sidebar) for one-click real exits - until then use the journal close below.")
        return
    km_ = kite_mcp_client()
    side_ = str(side).lower()
    verb = "BUY-to-close" if side_ == "short" else "SELL"
    mkt = kite_mcp.market_open_now()
    st.caption("This cancels any live GTT/stop order on " + sym + " for the account (all of them), then places "
               + (("a MARKET " + verb) if mkt else ("a " + verb + " LIMIT through the live price, so it fills")) +
               " for " + str(int(qty)) + " shares.")
    checked = st.checkbox("Confirm: exit " + sym + " x" + str(int(qty)) + " now", key=key + "_ok")
    if st.button("EXIT NOW (early exit)", type="primary", disabled=not checked, use_container_width=True, key=key + "_go"):
        msgs = []
        try:
            for g in orders.active_gtts(km_, sym):
                km_.delete_gtt(g.get("id"))
                msgs.append("cancelled GTT #" + str(g.get("id")))
        except Exception as e:
            msgs.append("GTT cancel issue: " + str(e))
        try:
            for o in orders.open_orders(km_, sym):
                km_.cancel_order(str(o.get("order_id")))
                msgs.append("cancelled order " + str(o.get("order_id")))
        except Exception as e:
            msgs.append("order cancel issue: " + str(e))
        px = None
        try:
            px = km_.ltp([sym]).get(sym)
        except Exception:
            pass
        px = px or ltp
        if not px:
            st.error("Could not fetch a live price - cancel/exit manually in Kite. " + "; ".join(msgs))
            return
        otype, lim = orders.exit_spec(side_, float(px))
        try:
            res = km_.place_order(tradingsymbol=sym, transaction_type=("BUY" if side_ == "short" else "SELL"),
                                  quantity=int(qty), product=("MIS" if side_ == "short" else "CNC"),
                                  order_type=otype, price=lim, tag="SQD-EXIT")
            oid = res.get("order_id") if isinstance(res, dict) else None
            if oid:
                orders.record("exit", oid, symbol=sym, side=side_, qty=int(qty), trade_id=int(tid))
            msgs.append("exit order " + str(oid or res) + " placed (" + otype +
                        ((" Rs " + fmt(lim)) if lim else "") + ")")
            st.success("; ".join(msgs))
            st.caption("Click 'Check orders / fills now' (My Trades tab) in a few seconds - the fill closes the "
                       "journal trade at the actual price.")
            live_ltp.clear()
        except Exception as e:
            st.error("Exit order failed: " + str(e) + ((" | " + "; ".join(msgs)) if msgs else ""))


def sync_gtt_stop(km_, sym, qty, tid, new_stop, last_price=None):
    """Move the lower (stop) leg of the first two-leg GTT on sym to new_stop and mirror
    it in the journal. Returns (ok, message)."""
    try:
        twoleg = [g for g in orders.active_gtts(km_, sym) if len(orders.gtt_legs(g)) >= 2]
        if not twoleg:
            return False, "No two-leg GTT found on Kite for " + sym + " - place protection first."
        g = twoleg[0]
        upper = max(l["trigger"] for l in orders.gtt_legs(g))
        lower_new = float(new_stop)
        if lower_new >= upper:
            return False, "New stop must stay below the target leg (Rs " + fmt(upper) + ")."
        lpx = km_.ltp([sym]).get(sym) or last_price or upper
        km_.modify_gtt(g.get("id"), tradingsymbol=sym, transaction_type="SELL",
                       product="CNC", trigger_type="two-leg", last_price=float(lpx),
                       upper_trigger_value=upper, upper_limit_price=round(upper * 0.995, 2),
                       upper_quantity=int(qty), lower_trigger_value=lower_new,
                       lower_limit_price=round(lower_new * 0.995, 2), lower_quantity=int(qty))
        engine.update_trade(int(tid), stop=lower_new)
        return True, "GTT stop on Kite moved to Rs " + fmt(lower_new) + "."
    except Exception as e:
        return False, "GTT modify failed: " + str(e)


settings = engine.load_settings()

with st.sidebar:
    st.title("Swing Desk")
    st.caption("Assisted NSE swing trading - you review every call; orders from the app are confirm-gated.")

    kc, kstatus = kite_client()
    if kc is not None:
        st.success("Kite: " + kstatus)
    else:
        st.info("Kite: " + kstatus)

    st.divider()
    st.subheader("Kite live feed + order actions")
    lstate, ldetail = kite_state()
    if lstate == "live":
        st.success("Live feed: " + ldetail)
        st.caption("Real-time Kite prices now power My Trades, the IPO trail check and the in-app order actions.")
    elif lstate == "login":
        st.warning("WARNING: AI systems are unpredictable and non-deterministic. By continuing, you agree "
                   "to interact with your Zerodha account via AI at your own risk.")
        st.caption("One-time login per app session. Prices, fill-sync and order actions go live after you "
                   "authorise - the app never sees your Zerodha password.")
        km_ = kite_mcp_client()
        if st.session_state.get("kite_login_sid") != km_.session_id:
            try:
                st.session_state["kite_login_url"] = km_.login_url()
                st.session_state["kite_login_sid"] = km_.session_id
            except Exception as e:
                st.session_state.pop("kite_login_url", None)
                st.caption("Login link failed: " + str(e))
        if st.session_state.get("kite_login_url"):
            st.link_button("Log in to Kite", st.session_state["kite_login_url"], use_container_width=True)
        if st.button("I've logged in - refresh feed", use_container_width=True):
            kite_state.clear()
            live_ltp.clear()
            live_quote.clear()
            st.session_state.pop("kite_login_url", None)
            st.session_state.pop("kite_login_sid", None)
            st.rerun()
    else:
        st.warning("Live feed unavailable: " + ldetail)
        if st.button("Retry live feed", use_container_width=True):
            kite_mcp_client.clear()
            kite_state.clear()
            st.rerun()

    st.divider()
    st.subheader("Risk settings")
    capital = st.number_input("Capital (Rs)", min_value=10000.0, value=float(settings["capital"]), step=10000.0)
    risk_pct = st.number_input("Risk per trade (%)", min_value=0.1, max_value=5.0,
                               value=float(settings["risk_pct"]), step=0.25, format="%.2f")
    tg1, tg2 = st.columns(2)
    trail_gate = tg1.number_input("Trail arms at (+R)", min_value=0.25, max_value=5.0,
                                  value=float(settings.get("trail_gate_r", 1.0)), step=0.25, format="%.2f")
    trail_gb = tg2.number_input("Trail giveback (%)", min_value=5.0, max_value=90.0,
                                value=float(settings.get("trail_giveback", 30.0)), step=5.0)
    st.caption("Giveback trail: once a trade is up " + format(trail_gate, ".2f") +
               "R, the suggested stop follows the best price and gives back only " +
               format(trail_gb, ".0f") + "% of the run. It never loosens.")
    if (capital != settings["capital"] or risk_pct != settings["risk_pct"]
            or trail_gate != settings.get("trail_gate_r") or trail_gb != settings.get("trail_giveback")):
        engine.save_settings({"capital": capital, "risk_pct": risk_pct,
                              "trail_gate_r": trail_gate, "trail_giveback": trail_gb})

    st.divider()
    st.subheader("Watchlist")
    wl_files = sorted(f for f in os.listdir(WATCHLIST_DIR) if f.endswith(".txt")) if os.path.isdir(WATCHLIST_DIR) else []
    wl_choice = st.selectbox("File", wl_files or ["(none)"])
    custom = st.text_area("Or paste symbols", placeholder="RELIANCE, TCS, KOTAKBANK ...", height=80)

    with st.expander("How plans are built"):
        st.markdown(
            "**Entry** current close. **Stop** 1.5 x ATR below entry (clamped 2-8%). "
            "**Target** 2R, stretch at 52-week high.\n\n"
            "**Size** = risk % x capital / stop distance, capped at 25% of capital per trade.\n\n"
            "**Quality**: trend stack (price>20>50>200), weekly trend, relative strength vs Nifty, "
            "volume, setup type. A >= 8, B >= 6, C >= 4.\n\n"
            "**Setups**: Breakout/momentum, Pullback in uptrend, Trend continuation, "
            "Oversold bounce (counter-trend, half conviction).\n\n"
            "**Decision desk**: one merged call per stock from the swing setup + today's "
            "news/results reaction + your open trades. MANAGE (open trade), BUY / SHORT "
            "(fast-action button logs it), or WAIT when nothing lines up."
        )

symbols = resolve_symbols(wl_choice, custom)

tab_desk, tab_ipo, tab_trades, tab_journal = st.tabs(
    ["Decision desk (scan + events)", "IPO desk (listing play)", "My Trades (fast out)", "Journal"])

with tab_desk:
    flash = st.session_state.pop("desk_flash", None)
    if flash:
        st.success(flash)
    st.markdown("#### One call per stock - scanner + news/results merged into a single verdict")
    st.caption("Per stock: trend setup + today's reaction to news/results + your open trades. "
               "Fast actions log to the journal; with the live feed connected you can also place the real "
               "order (or exit) from here - each action asks for an explicit confirmation first.")
    c1, c2, c3 = st.columns([1, 1, 4])
    run = c1.button("Run full scan", type="primary", use_container_width=True)
    fresh = c2.checkbox("Fresh prices", help="Ignore the intraday cache and re-download")

    if run:
        if not symbols:
            st.warning("No symbols. Pick a watchlist file or paste symbols in the sidebar.")
        else:
            pbar = st.progress(0.0, text="Starting...")
            rows = desk.desk_scan(symbols, capital, risk_pct, period="2y", hours=40, refresh=fresh,
                                  progress=lambda f, s: pbar.progress(min(f, 1.0), text="Analysing " + str(s)))
            pbar.empty()
            st.session_state["desk"] = {"rows": rows, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}

    saved = st.session_state.get("desk")
    if saved:
        rows = saved["rows"]
        rank = {"MANAGE": 0, "BUY": 1, "SHORT": 2}
        calls = [r for r in rows if r.get("decision") and r["decision"]["action"] in rank]
        calls.sort(key=lambda r: rank.get(r["decision"]["action"], 9))
        if calls:
            st.markdown("**Today's calls:** " + " | ".join(
                r["symbol"] + " - " + r["decision"]["action"] +
                ((" " + r["decision"]["side"]) if (r["decision"]["action"] == "MANAGE" and r["decision"].get("side")) else "") +
                ((" [" + str(r["decision"]["conviction"]) + "]") if r["decision"].get("conviction") else "")
                for r in calls))
        else:
            st.info("Nothing actionable right now - every stock is WAIT. Staying out is a position; "
                    "rescan after the next close or check the full table below for context.")

        table = []
        for r in rows:
            m, p, rx, pp, dec = (r.get("metrics"), r.get("plan"), r.get("react"),
                                 r.get("eplan"), r.get("decision"))
            if r.get("error") or m is None:
                table.append({"Symbol": r["symbol"], "Call": "no data: " + str(r.get("error"))})
                continue
            lev = (dec or {}).get("levels") or {}
            rew_r = rew_p = risk_p = None
            if dec and dec.get("side") and lev.get("trigger") is not None and lev.get("stop") is not None \
                    and lev.get("target") is not None and lev.get("qty"):
                prof_, upct_, _, lpct_ = reward_read(dec["side"], float(lev["trigger"]), float(lev["stop"]),
                                                    float(lev["target"]), int(lev["qty"]))
                rew_r, rew_p, risk_p = round(prof_, 0), round(upct_, 1), round(lpct_, 1)
            table.append({
                "Symbol": r["symbol"],
                "Call": dec["action"] if dec else "-",
                "Conv": (dec.get("conviction") or "") if dec else "",
                "CMP": round(m["close"], 2),
                "Trigger": None if lev.get("trigger") is None else round(lev["trigger"], 2),
                "Stop": None if lev.get("stop") is None else round(lev["stop"], 2),
                "Target": None if lev.get("target") is None else round(lev["target"], 2),
                "Qty": lev.get("qty"),
                "Risk Rs": None if lev.get("risk") is None else round(lev["risk"], 0),
                "Reward Rs": rew_r,
                "Reward %": rew_p,
                "Risk %": risk_p,
                "Setup": p.get("setup") or "no clean setup",
                "Qual": p.get("qual", "-"),
                "Event bias": (pp["bias"] if pp else "-"),
                "Reaction": rx["klass"] if rx else "-",
                "Vol x": round(rx["vol_ratio"], 1) if rx else None,
                "Day %": round(rx["chg"], 1) if rx else None,
                "Events": r["event_flags"],
                "ST": m["st_label"],
                "LT": m["lt_label"],
            })
        df = pd.DataFrame(table)
        shown = df
        if "Call" in df.columns:
            def ccolor(v):
                return {"BUY": "background-color: #c8f7c5; color: #14532d; font-weight: 600;",
                        "SHORT": "background-color: #f8d7da; color: #7f1d1d; font-weight: 600;",
                        "MANAGE": "background-color: #dbeafe; color: #1e3a8a; font-weight: 600;",
                        "WAIT": "background-color: #f1f5f9; color: #64748b;"}.get(str(v), "")
            def qcolor(v):
                return {"A": "background-color: #c8f7c5; color: #14532d;",
                        "B": "background-color: #e6f7c8; color: #365314;",
                        "C": "background-color: #fdf3c8; color: #713f12;",
                        "weak": "background-color: #f8d7da; color: #7f1d1d;"}.get(str(v), "")
            try:
                shown = df.style.map(ccolor, subset=["Call"]).map(qcolor, subset=["Qual"])
            except Exception:
                shown = df
        st.dataframe(shown, use_container_width=True, hide_index=True)
        st.caption("Scan at " + saved["at"] + " | prices via Yahoo (delayed)" +
                   (" - live Kite prices show in the detail panel." if lstate == "live" else "") +
                   " Verify on Kite before ordering. "
                   "Equity shorts are MIS (intraday) only - square off by 3:20 PM; overnight shorts need futures.")

        names = [r["symbol"] for r in rows if r.get("metrics")]
        if names:
            pick = st.selectbox("Stock detail - full decision, news and fast actions", ["-"] + names, key="desk_pick")
            if pick and pick != "-":
                r = next(x for x in rows if x["symbol"] == pick)
                m, p, rx, pp, dec = (r["metrics"], r["plan"], r["react"], r["eplan"], r["decision"])
                lev = (dec.get("levels") or {}) if dec else {}
                st.divider()
                st.markdown("### " + pick + " - " + (dec["headline"] if dec else "-"))
                ls = level_strip(dec) if dec else None
                if ls:
                    st.caption(ls)
                left, right = st.columns([3, 2])
                with left:
                    try:
                        dfd, ym, _ = engine.fetch(pick, "2y")
                        if dfd is not None:
                            d = engine.add_indicators(dfd)
                            if dec and dec.get("source") == "swing" and p and p.get("tradeable"):
                                cp = p
                            elif lev:
                                cp = {"entry": lev.get("trigger"), "stop": lev.get("stop"),
                                      "target": lev.get("target"), "stretch": lev.get("stretch")}
                            else:
                                cp = None
                            st.plotly_chart(make_detail_chart(d, cp), use_container_width=True)
                    except Exception as e:
                        st.error("Chart failed: " + str(e))
                    if rx:
                        st.write("**Today's candle:** open " + fmt(rx["open"]) + " | high " + fmt(rx["high"]) +
                                 " | low " + fmt(rx["low"]) + " | close " + fmt(rx["close"]) +
                                 " (day " + format(rx["chg"], "+.2f") + "%, gap " + format(rx["gap"], "+.2f") + "%)")
                        st.write("Volume " + format(rx["vol_ratio"], ".1f") + "x normal | range " +
                                 format(rx["rng_atr"], ".1f") + "x ATR | closed at " +
                                 format(rx["close_pos"] * 100, ".0f") + "% of the day's range")
                    if lstate == "live":
                        dlq = live_quote(pick)
                        if dlq:
                            lp_ = dlq.get("last_price")
                            oh_ = dlq.get("ohlc") or {}
                            pc_ = oh_.get("close")
                            chg_ = (lp_ / pc_ - 1) * 100 if (lp_ and pc_) else None
                            st.write("**Live now (Kite):** Rs " + fmt(lp_) +
                                     (" (" + format(chg_, "+.2f") + "% vs prev close)" if chg_ is not None else "") +
                                     " | day high Rs " + fmt(oh_.get("high")) + " / low Rs " + fmt(oh_.get("low")))
                with right:
                    st.markdown("##### Why this call")
                    for why in (dec["reasons"] if dec else []):
                        st.write("- " + why)
                    for wmark in (dec["warnings"] if dec else []):
                        st.warning(wmark)
                    st.markdown("##### Fast actions")
                    if dec and dec["action"] == "MANAGE" and dec.get("trade_id") is not None:
                        ltp = float(m["close"])
                        entry_, stop_, tgt_ = float(lev["trigger"]), float(lev["stop"]), float(lev["target"])
                        qty_ = int(lev["qty"])
                        pnl = ((entry_ - ltp) if dec["side"] == "short" else (ltp - entry_)) * qty_
                        act, r_now, dist = exit_read(dec["side"], entry_, stop_, tgt_, ltp)
                        st.write("Open " + str(dec["side"]) + " " + str(qty_) + " @ Rs " + fmt(entry_) +
                                 " | stop Rs " + fmt(stop_) + " | target Rs " + fmt(tgt_))
                        st.write("Last Rs " + fmt(ltp) + " | P&L " + format(pnl, "+,.0f") +
                                 (" | R " + format(r_now, "+.2f") if r_now is not None else ""))
                        st.info(act)
                        jt = engine.load_journal()
                        jt = jt[jt["id"] == int(dec["trade_id"])]
                        ext_, esrc_ = trade_extreme(dec["side"], pick, str(jt["date_in"].iloc[0]) if len(jt) else "")
                        if ext_ is not None:
                            tp_ = engine.trail_plan(dec["side"], entry_, stop_, ext_, qty_,
                                                    giveback_pct=float(settings.get("trail_giveback", 30.0)),
                                                    gate_r=float(settings.get("trail_gate_r", 1.0)))
                            st.caption(("Trail" if esrc_ == "kite" else "Trail (Yahoo bars)") + ": " + tp_["reason"] +
                                       (" Apply it in My Trades > Manage." if tp_["improves"] else ""))
                        else:
                            st.caption("Trail unavailable: " + str(esrc_))
                        prof, upct, loss, lpct = reward_read(dec["side"], entry_, stop_, tgt_, qty_)
                        st.markdown("If target hits: :green[**+Rs " + format(prof, ",.0f") + " (+" +
                                    format(upct, ".1f") + "%)**] | If stop hits: :red[**-Rs " +
                                    format(loss, ",.0f") + " (-" + format(lpct, ".1f") + "%)**] (vs entry Rs " +
                                    fmt(entry_) + ")")
                        if st.button("LOG EXIT at Rs " + fmt(ltp), type="primary", use_container_width=True):
                            engine.close_trade(int(dec["trade_id"]), float(ltp))
                            st.session_state["desk_flash"] = ("Closed trade " + str(dec["trade_id"]) + " at Rs " +
                                                              fmt(ltp) + " - see the Journal tab for stats.")
                            st.rerun()
                        st.caption("The button above updates only the journal. To also exit for real:")
                        with st.expander("Exit in Kite now (real order)"):
                            exit_now_block(int(dec["trade_id"]), pick, dec["side"], int(qty_), ltp, lstate,
                                           key="desk_exit_" + pick)
                    elif dec and dec["action"] in ("BUY", "SHORT") and lev.get("trigger") is not None:
                        trig = lev.get("trigger")
                        long_side = dec["side"] == "long"
                        prof, upct, loss, lpct = reward_read(dec["side"], float(trig), float(lev["stop"]),
                                                             float(lev["target"]), int(lev["qty"]))
                        st.markdown("If target hits: :green[**+Rs " + format(prof, ",.0f") + " (+" +
                                    format(upct, ".1f") + "%)**] | If stop hits: :red[**-Rs " +
                                    format(loss, ",.0f") + " (-" + format(lpct, ".1f") + "%)**] (vs entry Rs " +
                                    fmt(trig) + ")")
                        btn = ("LOG ENTRY: " + ("BUY " if long_side else "SELL SHORT ") + str(int(lev["qty"])) +
                               " @ Rs " + fmt(trig))
                        if st.button(btn, type="primary", use_container_width=True):
                            if dec.get("source") == "event":
                                src_setup = "Event: " + (rx["klass"] if rx else "")
                                src_qual = pp["bias"] if pp else ""
                            else:
                                src_setup = (p.get("setup") or "") if p else ""
                                src_qual = (p.get("qual") or "") if p else ""
                            engine.add_trade(pick, int(lev["qty"]), round(trig, 2), round(lev["stop"], 2),
                                             round(lev["target"], 2), src_setup, str(src_qual),
                                             "decision desk", side=dec["side"])
                            st.session_state["desk_flash"] = ("Logged " + ("BUY " if long_side else "SHORT ") +
                                                              str(int(lev["qty"])) + " " + pick + " @ Rs " + fmt(trig) +
                                                              " as OPEN - track it in My Trades. Rescan to refresh calls.")
                            st.rerun()
                        if long_side:
                            st.caption("Manual route: CNC LIMIT at/below Rs " + fmt(trig) + " - no chase beyond +1%.")
                        else:
                            st.caption("Manual route: MIS stop-loss order below Rs " + fmt(trig) +
                                       " - intraday only, square off by 3:20 PM.")
                        with st.expander("Place this trade in Kite (real order)"):
                            place_entry_form(pick, dec, lev, p, pp, rx, lstate, key="desk_place_" + pick)
                    else:
                        st.caption("Nothing to do on this one. " +
                                   ("Levels shown are reference only - the conditions for a trade were not met."
                                    if lev else "No trade candidates at current levels."))
                n1, n2 = st.columns([3, 2])
                with n1:
                    if r["news"]:
                        st.markdown("**News (last 40h, IST):**")
                        for nw in r["news"][:6]:
                            ts = nw["time_ist"].strftime("%d %b %H:%M") if nw["time_ist"] is not None else ""
                            title = nw["title"]
                            link = nw["link"]
                            st.markdown("- " + (("[" + title + "](" + link + ")") if link else title) +
                                        " - " + str(nw["publisher"]) + " " + ts + " (" + nw["sentiment"] + ")")
                    else:
                        st.write("No news in the last 40 hours (Yahoo feed).")
                with n2:
                    earn = r["earn"] or {}
                    einfo = []
                    if earn.get("results_today"):
                        einfo.append("results reported " + str(earn["results_today"]))
                    if earn.get("next_date"):
                        einfo.append("next results " + str(earn["next_date"]))
                    if earn.get("eps_actual") is not None:
                        s_ = "EPS " + format(earn["eps_actual"], ".2f") + " vs est " + \
                             (format(earn["eps_est"], ".2f") if earn.get("eps_est") is not None else "?")
                        if earn.get("surprise") is not None:
                            s_ += " (surprise " + format(earn["surprise"], "+.1f") + "%)"
                        einfo.append(s_)
                    if einfo:
                        st.markdown("**Results:** " + "; ".join(einfo))
                    if pp:
                        st.caption(pp["rule"])
                if p and p.get("tradeable"):
                    with st.expander("Swing order ticket (CNC, delivery)"):
                        st.code(order_ticket(pick, p, capital, risk_pct), language=None)
                        with st.form("desk_log_swing_" + pick):
                            note = st.text_input("Note (optional)")
                            if st.form_submit_button("Log swing LONG as OPEN"):
                                engine.add_trade(pick, p["qty"], round(p["entry"], 2), round(p["stop"], 2),
                                                 round(p["target"], 2), p.get("setup", ""), p.get("qual", ""), note)
                                st.success("Logged to journal - track it in My Trades.")
                with st.expander("Event tickets - both sides (trigger-conditional)",
                                 expanded=(bool(dec) and dec.get("source") == "event")):
                    if pp:
                        t1, t2 = st.columns(2)
                        with t1:
                            st.markdown("**Long ticket - only if strength at open**")
                            lp = pp["long"]
                            st.code(event_ticket(pick, "long", lp), language=None)
                            with st.form("desk_ev_long_" + pick):
                                if st.form_submit_button("Log LONG at trigger"):
                                    engine.add_trade(pick, lp["qty"], round(lp["trigger"], 2), round(lp["stop"], 2),
                                                     round(lp["target"], 2), "Event: " + (rx["klass"] if rx else ""),
                                                     pp["bias"], "decision desk", side="long")
                                    st.success("Logged long to journal.")
                        with t2:
                            st.markdown("**Short ticket - only if weakness at open (MIS intraday)**")
                            sp2 = pp["short"]
                            st.code(event_ticket(pick, "short", sp2), language=None)
                            with st.form("desk_ev_short_" + pick):
                                if st.form_submit_button("Log SHORT at trigger"):
                                    engine.add_trade(pick, sp2["qty"], round(sp2["trigger"], 2), round(sp2["stop"], 2),
                                                     round(sp2["target"], 2), "Event: " + (rx["klass"] if rx else ""),
                                                     pp["bias"], "decision desk", side="short")
                                    st.success("Logged short to journal.")
                    else:
                        st.caption("No significant event reaction for this stock - nothing to prepare for the open.")
                with st.expander("Signal detail"):
                    st.write("- RSI(14): " + fmt(m["rsi"], 1) + " | MACD hist: " + fmt(m["hist"], 2) +
                             " | ATR%: " + fmt(m["atr"] / m["close"] * 100, 2))
                    st.write("- 200-DMA: Rs " + fmt(m["sma200"]) +
                             (" (" + format((m["close"] / m["sma200"] - 1) * 100, "+.1f") + "%)" if m["sma200"] else ""))
                    st.write("- 52w range: Rs " + fmt(m["lo52"]) + " - Rs " + fmt(m["hi52"]) +
                             (" | at " + fmt(m["pos52"], 0) + "% of range" if m["pos52"] is not None else ""))
                    st.write("- 3-mo support / resistance: Rs " + fmt(m["sup3"]) + " / Rs " + fmt(m["res3"]))
                    if m.get("rs63") is not None:
                        st.write("- Relative strength vs Nifty (3m): " + format(m["rs63"], "+.1f") + "%")
                    for n in (p.get("notes") or []) + (m.get("notes") or []):
                        st.write("- " + n)
    else:
        st.info("Pick a watchlist in the sidebar and hit **Run full scan** - you get one call per stock "
                "(MANAGE / BUY / SHORT / WAIT) with everything behind it in one panel.")

with tab_ipo:
    st.markdown("#### IPO desk - play the listing day (fast in, trail out)")
    st.caption("Only high-GMP issues get a ticket: enter near the GMP-implied price as it lists with a hard stop, then ride it "
               "with a ratcheting trail that gives back only ~30% of the max profit (peak Rs 120 on entry Rs 100 -> stop Rs 114). "
               "GMP is the unofficial grey-market premium - it can change or break between close and listing. "
               "Data: investorgain.com live boards (cached 20 min). Not advice - verify on NSE/Kite.")
    i1, i2, i3 = st.columns([1, 1, 4])
    run_ipo = i1.button("Scan IPO board", type="primary", use_container_width=True)
    fresh_ipo = i2.checkbox("Fresh IPO data", help="Ignore the 20-minute cache and re-pull")

    if run_ipo:
        try:
            with st.spinner("Pulling GMP board, subscriptions and the ~250-listing track record..."):
                b = ipo.board(refresh=fresh_ipo)
                subs = ipo.subscription(refresh=fresh_ipo)
                perf = ipo.performance(refresh=fresh_ipo)
            st.session_state["ipo_scan"] = {"board": b, "subs": subs, "perf": perf,
                                            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        except Exception as e:
            st.error("IPO fetch failed: " + str(e) + " - investorgain.com may be slow or a report moved. Try again shortly.")

    iscan = st.session_state.get("ipo_scan")
    if iscan:
        b, subs, perf = iscan["board"], iscan["subs"], iscan["perf"]
        sp = ipo.board_split(b)
        buckets, head = ipo.bucket_stats(perf)

        scored = []
        for r in b:
            match = ipo.match_subs(r["name"], subs)
            s, lab, notes = ipo.score(r, match)
            rr = dict(r)
            rr["_sub_match"] = match
            rr["_score"] = s
            rr["_lab"] = lab
            rr["_notes"] = notes
            scored.append(rr)
        by_id = {r["id"]: r for r in scored}

        def vcolor(v):
            v = str(v)
            if v.startswith("A"):
                return "background-color: #c8f7c5; color: #14532d; font-weight: 600;"
            if v.startswith("B"):
                return "background-color: #e6f7c8; color: #365314;"
            if v.startswith("C"):
                return "background-color: #fdf3c8; color: #713f12;"
            return "background-color: #f1f5f9; color: #64748b;"

        def gcolor(v):
            if v is None:
                return ""
            if v >= 20:
                return "background-color: #c8f7c5; color: #14532d; font-weight: 600;"
            if v >= 10:
                return "background-color: #e6f7c8; color: #365314;"
            if v >= 5:
                return "background-color: #fdf3c8; color: #713f12;"
            return ""

        st.markdown("##### Listing soon - the tickets (closed issues waiting to list)")
        soon = sorted((by_id[r["id"]] for r in sp["soon"]), key=lambda x: -x["_score"])
        playable = [r for r in soon if r["_score"] >= 5 and r.get("gmp_pct") is not None and r["gmp_pct"] >= 10]
        if playable:
            st.markdown("**Trade candidates:** " + " | ".join(
                r["name"] + " (" + r["cat"] + ", lists " + str(r["listing_dt"]) + ") - GMP Rs " +
                fmt(r["gmp_rs"], 1) + " (" + format(r["gmp_pct"], ".1f") + "%), score " + str(r["_score"])
                for r in playable))
        else:
            st.info("No listing-day candidates right now - nothing closed with GMP >= 10%. When nothing qualifies, "
                    "the desk stays flat. Check again after the next subscription rounds close.")
        if soon:
            t = []
            for r in soon:
                m = r.get("_sub_match") or {}
                dd = (r["listing_dt"] - dt.date.today()).days if r["listing_dt"] else None
                t.append({
                    "Name": r["name"], "Cat": r["cat"],
                    "Lists": (str(r["listing_dt"]) + (" (today)" if dd == 0 else (" (tomorrow)" if dd == 1 else "")))
                             if r["listing_dt"] else "-",
                    "Issue Rs": r["price"], "GMP Rs": r["gmp_rs"], "GMP %": r["gmp_pct"],
                    "Sub x": m.get("total") if m.get("total") is not None else r["sub"],
                    "QIB x": m.get("qib"), "Size Cr": r["size_cr"], "Lot": r["lot"],
                    "Score": r["_score"], "Verdict": r["_lab"],
                })
            sdf = pd.DataFrame(t)
            try:
                shown_s = sdf.style.map(vcolor, subset=["Verdict"]).map(gcolor, subset=["GMP %"])
            except Exception:
                shown_s = sdf
            st.dataframe(shown_s, use_container_width=True, hide_index=True)

            soon_names = [r["name"] for r in soon]
            pick_s = st.selectbox("Listing-day plan", ["-"] + soon_names, key="ipo_pick_soon")
            if pick_s and pick_s != "-":
                r = next(x for x in soon if x["name"] == pick_s)
                st.markdown("### " + r["name"] + " (" + r["cat"] + ") - " + r["_lab"])
                st.caption("Lists " + str(r["listing_dt"]) + " | issue Rs " + fmt(r["price"]) + " | GMP Rs " +
                           fmt(r["gmp_rs"], 1) + " (" + format(r["gmp_pct"] or 0, ".1f") + "%) | peak GMP Rs " +
                           fmt(r["gmp_peak"], 1) + " | sub " + fmt(r["sub"], 1) + "x")
                left, right = st.columns([3, 2])
                with left:
                    st.markdown("**Why:**")
                    for n in r["_notes"]:
                        st.write("- " + n)
                    bl = ipo_bucket_line(r.get("gmp_pct"), buckets)
                    if bl:
                        st.caption(bl)
                    m = r.get("_sub_match")
                    if m and m.get("total") is not None:
                        st.write("**Subscription:** total " + fmt(m.get("total"), 1) + "x | QIB " +
                                 fmt(m.get("qib"), 1) + "x | NII " + fmt(m.get("nii"), 1) + "x | retail " +
                                 fmt(m.get("retail"), 1) + "x" + (" | anchor book filled" if m.get("anchor") else ""))
                    st.markdown("[Details on investorgain](" + r["url"] + ")")
                with right:
                    st.markdown("**The play**")
                    giveback = st.number_input("Trail giveback (% of max profit)", min_value=5.0, max_value=60.0,
                                               value=30.0, step=5.0, key="ipo_giveback",
                                               help="Your exit rule: stop = peak - giveback% x (peak - entry). "
                                                    "At 30%: entry 100, peak 120 -> stop 114; peak 130 -> stop 121.")
                    plan = ipo.listing_plan(r, capital, risk_pct, giveback)
                    if plan and plan["qty"] > 0:
                        st.code(ipo_ticket(r, plan), language=None)
                        st.caption("Place the CNC delivery order in Kite only after it lists and trades inside the zone. "
                                   "The initial stop is mandatory; then manage the trade with the trail ladder below - "
                                   "Kite stops don't ratchet by themselves, so raise the SL-M/GTT after each new high.")
                        with st.expander("Place buy order (CNC delivery)", key="ipo_order_exp_" + pick_s):
                            _is_sme = bool(r.get("cat") == "SME") or bool(r.get("lot", 0) > 1)
                            _lot_sz = int(r.get("lot") or 1)
                            if _is_sme:
                                st.caption("SME issue — trades in lots of {} shares on NSE Emerge. "
                                           "Minimum order = {}, multiples of {}."
                                           .format(_lot_sz, _lot_sz, _lot_sz))
                            _sym = st.text_input("Trading symbol on NSE",
                                                 value=r["name"], key="ipo_osym_" + pick_s)
                            c1, c2 = st.columns(2)
                            with c1:
                                _qty = st.number_input("Quantity",
                                                       min_value=_lot_sz if _is_sme else 1,
                                                       step=_lot_sz if _is_sme else 1,
                                                       value=max(int(plan["qty"]), _lot_sz if _is_sme else 1),
                                                       key="ipo_oqty_" + pick_s)
                            with c2:
                                _otype = st.selectbox("Order type", ["LIMIT", "MARKET"],
                                                       key="ipo_otype_" + pick_s)
                            _price = st.number_input("Limit price (ignored for MARKET)", min_value=0.01,
                                                     value=float(plan["entry"]), key="ipo_oprice_" + pick_s)
                            _cost = float(_qty) * float(_price)
                            _cpct = _cost / capital * 100 if capital else 0
                            st.caption("Order Rs {:,.0f} ({:.1f}% of capital)".format(_cost, _cpct))
                            if _cost > capital * risk_pct / 100:
                                st.warning("Above your risk budget of Rs {:,.0f} — this lot exceeds "
                                           "your normal {}% risk limit."
                                           .format(capital * risk_pct / 100, risk_pct))
                            _ok = st.checkbox("I confirm this places a real order in my Zerodha account",
                                              key="ipo_oplace_ok_" + pick_s)
                            if st.button("PLACE BUY ORDER", type="primary", disabled=not _ok,
                                          use_container_width=True, key="ipo_oplace_btn_" + pick_s):
                                _km = kite_mcp_client()
                                _st, _det = _km.status()
                                if _st != "live":
                                    st.error("Not connected to Kite — log in from the sidebar first.")
                                else:
                                    try:
                                        _res = _km.place_order(
                                            tradingsymbol=str(_sym).strip().upper(),
                                            transaction_type="BUY",
                                            quantity=int(_qty),
                                            product="CNC",
                                            order_type=str(_otype),
                                            price=float(_price) if _otype == "LIMIT" else None,
                                            tag="SQD-IPO")
                                        _oid = _res.get("order_id") if isinstance(_res, dict) else None
                                        st.success("Order placed" + ("! ID: " + str(_oid) if _oid else "") +
                                                   " — track it in My Trades > Orders placed from the app.")
                                    except Exception as _ex:
                                        st.error("Order failed: " + str(_ex))
                        with st.expander("After you actually buy - log it to the journal"):
                            with st.form("ipo_log_" + pick_s):
                                sym_in = st.text_input("NSE symbol in Kite", value=r["name"])
                                qty_in = st.number_input("Qty", min_value=1, value=max(int(plan["qty"]), 1))
                                entry_in = st.number_input("Entry price", min_value=0.01, value=float(plan["entry"]))
                                stop_in = st.number_input("Stop", min_value=0.01, value=float(plan["stop"]))
                                tgt_in = st.number_input("Target", min_value=0.01, value=float(plan["target"]))
                                if st.form_submit_button("Log IPO trade as OPEN"):
                                    engine.add_trade(sym_in.strip().upper(), int(qty_in), float(entry_in),
                                                     float(stop_in), float(tgt_in),
                                                     "IPO listing play (GMP " + format(r["gmp_pct"] or 0, ".0f") + "%)",
                                                     r["_lab"], "IPO desk", side="long")
                                    st.success("Logged - track it in My Trades; exit guidance starts from the first quote.")
                    elif plan:
                        for f in plan["flags"]:
                            st.warning(f)
                        st.caption("Levels (reference): expected listing Rs " + fmt(plan["exp_listing"]) +
                                   " | stop Rs " + fmt(plan["stop"]) + " | trail gives back " +
                                   format(plan["giveback_pct"], ".0f") + "% of max profit")
                    else:
                        st.info("No issue price / GMP data - cannot size the play.")

                if plan and plan["qty"] > 0:
                    st.divider()
                    st.markdown("**Exit plan - trail that gives back at most " + format(giveback, ".0f") +
                                "% of the max profit**")
                    ex1, ex2 = st.columns([3, 2])
                    with ex1:
                        st.dataframe(pd.DataFrame([{
                            "Peak Rs": x["peak"], "Peak %": x["peak_pct"],
                            "Profit at peak Rs": x["profit_at_peak"], "Stop Rs": x["stop"],
                            "Locked Rs": x["locked_rs"], "Locked %": x["locked_pct"],
                        } for x in plan["trail"]]), use_container_width=True, hide_index=True)
                        st.caption("Entry Rs " + fmt(plan["entry"]) + ". Each new high lifts the stop and it never moves "
                                   "back down - you exit only when the trail stop is hit.")
                    with ex2:
                        st.markdown("**Listing-day trail check**")
                        if lstate == "live":
                            st.text_input("NSE symbol for the live quote (Kite tradingsymbol)",
                                          key="ipo_lsym_" + pick_s, placeholder="e.g. ORIENTCAB")
                            if st.button("Get live quote", key="ipo_lq_" + pick_s):
                                ksym = str(st.session_state.get("ipo_lsym_" + pick_s) or "").strip().upper()
                                st.session_state["ipo_lq_data_" + pick_s] = live_quote(ksym) if ksym else None
                            ilq = st.session_state.get("ipo_lq_data_" + pick_s)
                            if ilq:
                                ioh = ilq.get("ohlc") or {}
                                st.write("Live Rs " + fmt(ilq.get("last_price")) +
                                         " | day high Rs " + fmt(ioh.get("high")) + " / low Rs " + fmt(ioh.get("low")))
                                if ioh.get("high") and st.button("Use day high as peak", key="ipo_uh_" + pick_s):
                                    st.session_state["ipo_peak_" + pick_s] = float(ioh["high"])
                                    st.rerun()
                        pk_in = st.number_input("Highest price seen since entry (Rs)", min_value=0.0, value=0.0,
                                                step=0.5, key="ipo_peak_" + pick_s)
                        if pk_in and float(pk_in) > 0:
                            entry_f = float(plan["entry"])
                            pk_f = float(pk_in)
                            if pk_f <= entry_f:
                                st.info("Peak is still at/below entry Rs " + fmt(entry_f) +
                                        " - keep the initial hard stop at Rs " + fmt(plan["stop"]) + ".")
                            else:
                                cur_stop = ipo.trail_stop(entry_f, pk_f, float(giveback), float(plan["stop"]))
                                st.success("Raise the stop to Rs " + fmt(cur_stop) + " - locks Rs " +
                                           format((cur_stop - entry_f) * int(plan["qty"]), ",.0f") +
                                           " even if it reverses now. Never move it down.")
                        st.caption("Update the SL-M / GTT in Kite yourself - Kite stops don't trail automatically.")

        st.divider()
        st.markdown("##### Bidding open now")
        st.caption("If you like the odds you can ALSO apply while the issue is open (UPI mandate in your broker app) - "
                   "allocation is a lottery. The desk play is still the listing-day entry above.")
        if sp["open"]:
            t = []
            for r in sorted((by_id[x["id"]] for x in sp["open"]), key=lambda x: -x["_score"]):
                m = r.get("_sub_match") or {}
                dd = (r["close_dt"] - dt.date.today()).days if r["close_dt"] else None
                t.append({
                    "Name": r["name"], "Cat": r["cat"],
                    "Closes": (str(r["close_dt"]) + (" (today)" if dd == 0 else
                               (" (" + str(dd) + "d left)" if dd is not None and dd > 0 else "")))
                              if r["close_dt"] else "-",
                    "Issue Rs": r["price"], "GMP Rs": r["gmp_rs"], "GMP %": r["gmp_pct"],
                    "Sub x": m.get("total") if m.get("total") is not None else r["sub"],
                    "QIB x": m.get("qib"), "Size Cr": r["size_cr"], "Lot": r["lot"],
                    "Score": r["_score"], "Verdict": r["_lab"],
                })
            odf = pd.DataFrame(t)
            try:
                shown_o = odf.style.map(vcolor, subset=["Verdict"]).map(gcolor, subset=["GMP %"])
            except Exception:
                shown_o = odf
            st.dataframe(shown_o, use_container_width=True, hide_index=True)
        else:
            st.info("No issues open for bidding right now.")

        st.divider()
        st.markdown("##### Track record - how GMP translated into listing outcomes (last " + str(len(perf)) + " listings)")
        if head:
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Listings (GMP >= 10%)", str(head["n"]))
            c2.metric("Opened positive", format(head["listing_pos_pct"], ".0f") + "%")
            c3.metric("Median listing gain", format(head["listing_median"], "+.1f") + "%")
            c4.metric("Median at-listing -> close", format(head["drift_median"], "+.1f") + "%")
            c5.metric("Worst at-listing -> close", format(head["drift_worst"], "+.1f") + "%")
        if buckets:
            st.dataframe(pd.DataFrame([{
                "GMP bucket": x["bucket"], "n": x["n"], "Median listing %": x["listing_median"],
                "% listed positive": x["listing_pos_pct"], "Median buy -> close %": x["drift_median"],
                "% close above listing": x["drift_pos_pct"], "Worst drift %": x["drift_worst"],
            } for x in buckets]), use_container_width=True, hide_index=True)
        l1, l2 = st.columns([3, 2])
        with l1:
            def _ldate(r):
                try:
                    return dt.datetime.strptime(str(r["listing_dt"]), "%d-%b-%y").date()
                except Exception:
                    return dt.date(1970, 1, 1)
            rec = sorted([r for r in perf if r["gmp_pct"] is not None and r["listing_gain"] is not None],
                         key=_ldate, reverse=True)[:25]
            st.dataframe(pd.DataFrame([{
                "Name": r["name"], "Cat": r["cat"], "Listed": r["listing_dt"],
                "GMP %": r["gmp_pct"], "Listing gain %": r["listing_gain"],
                "close - listing %": r["drift"], "Sub x": r["sub"],
            } for r in rec]), use_container_width=True, hide_index=True)
        with l2:
            try:
                st.plotly_chart(ipo_scatter(perf), use_container_width=True)
            except Exception:
                pass
        st.caption("Read: above ~10% GMP the listing almost always opens positive and the median at-listing -> close drift is ~+6%; "
                   "below that the edge is gone. The drift is what the listing play harvests - the trail keeps most of a first-day "
                   "spike even when it fades (it gives back only the set % of the max profit), and the hard stop caps the worst case. "
                   "SME issues pop bigger but trade in 5% circuits with lot-size minimums - size small or skip. GMP is unofficial: odds, not guarantees. "
                   "Board pulled at " + iscan["at"] + ".")
    else:
        st.info("Hit **Scan IPO board** - you get: listing-soon tickets (only GMP >= 10% qualify), "
                "issues open for bidding, and the empirical track record behind the play.")

with tab_trades:
    st.markdown("#### Open swings - exit guidance")
    tflash = st.session_state.pop("trades_flash", None)
    if tflash:
        st.success(tflash)
    jdf = engine.load_journal()
    open_df = jdf[jdf["status"] == "open"] if len(jdf) else jdf
    if len(open_df):
        with st.spinner("Fetching latest prices..."):
            syms_open = tuple(str(s) for s in open_df["symbol"].tolist())
            live_px = live_ltp(syms_open)
            live = []
            for _, row in open_df.iterrows():
                sym = row["symbol"]
                ltp = live_px.get(sym)
                if ltp is None:
                    try:
                        dfx, _, _ = engine.fetch(sym, "5d", refresh=True)
                        if dfx is not None and len(dfx):
                            ltp = float(dfx["Close"].iloc[-1])
                    except Exception:
                        pass
                entry, stop, target, qty = float(row["entry"]), float(row["stop"]), float(row["target"]), float(row["qty"])
                side = str(row.get("side") or "long").lower()
                r_now = dist_stop = None
                if ltp is None:
                    action, scolor = "no price", "#e5e7eb"
                else:
                    action, r_now, dist_stop = exit_read(side, entry, stop, target, ltp)
                    if action.startswith("EXIT"):
                        scolor = "#f8d7da"
                    elif action.startswith("Target"):
                        scolor = "#c8f7c5"
                    elif action.startswith("Move stop"):
                        scolor = "#fdf3c8"
                    elif action.startswith("Near stop"):
                        scolor = "#fde8c8"
                    else:
                        scolor = "#e8f0fe"
                live.append({
                    "id": row["id"], "Symbol": sym, "Side": side, "Qty": int(qty), "Entry": round(entry, 2),
                    "Stop": round(stop, 2), "Target": round(target, 2),
                    "LTP": None if ltp is None else round(ltp, 2),
                    "P&L Rs": None if ltp is None else round(((ltp - entry) if side != "short" else (entry - ltp)) * qty, 0),
                    "R now": None if r_now is None else round(r_now, 2),
                    "Dist to stop %": None if dist_stop is None else round(dist_stop, 2),
                    "Action": action, "_color": scolor,
                })
        live_df = pd.DataFrame(live)
        alerts = live_df[live_df["Action"].str.startswith("EXIT")]
        if len(alerts):
            st.error("Action needed: " + ", ".join(alerts["Symbol"].tolist()) + " at/below stop. Consider exiting in Kite now.")
        view = live_df.drop(columns=["_color"])
        try:
            styled = view.style.apply(lambda row: ["background-color: " + live_df.loc[row.name, "_color"]] * len(row), axis=1)
            st.dataframe(styled, use_container_width=True, hide_index=True)
        except Exception:
            st.dataframe(view, use_container_width=True, hide_index=True)
        st.caption("Prices " + ("LIVE from Kite" if lstate == "live" else "delayed (Yahoo) - connect the live feed in the sidebar") +
                   ". Stops and targets come from your journal. Manage below to update levels, protect on Kite, or exit for real.")
        with st.expander("Manage an open trade - levels, protection, early exit"):
            mg_tid = st.selectbox("Trade", live_df["id"].tolist(), key="mg_tid",
                                  format_func=lambda t: "#" + str(t) + "  " +
                                  str(live_df.loc[live_df['id'] == t, 'Symbol'].iloc[0]) + " (" +
                                  str(live_df.loc[live_df['id'] == t, 'Side'].iloc[0]) + " x" +
                                  str(live_df.loc[live_df['id'] == t, 'Qty'].iloc[0]) + ")")
            mgrow = live_df[live_df["id"] == mg_tid].iloc[0]
            msym, mside, mqty = str(mgrow["Symbol"]), str(mgrow["Side"]), int(mgrow["Qty"])
            mstop, mtgt, mltp = float(mgrow["Stop"]), float(mgrow["Target"]), mgrow["LTP"]
            u1, u2, u3 = st.columns([2, 2, 1])
            nstop = u1.number_input("Stop (Rs)", min_value=0.01, value=round(mstop, 2), step=0.05, key="mg_stop")
            ntgt = u2.number_input("Target (Rs)", min_value=0.01, value=round(mtgt, 2), step=0.05, key="mg_tgt")
            if u3.button("Update", key="mg_save") and (nstop != mstop or ntgt != mtgt):
                engine.update_trade(int(mg_tid), stop=float(nstop), target=float(ntgt))
                st.success("Journal levels updated. If a GTT protects this trade on Kite, sync it below so the exchange stops match.")
            trow = open_df[open_df["id"] == mg_tid].iloc[0]
            text_, tsrc = trade_extreme(mside, msym, str(trow.get("date_in") or ""))
            if text_ is not None:
                tp = engine.trail_plan(mside, float(mgrow["Entry"]), mstop, float(text_), mqty,
                                       giveback_pct=float(settings.get("trail_giveback", 30.0)),
                                       gate_r=float(settings.get("trail_gate_r", 1.0)))
                st.markdown("**Giveback trail** (" + format(float(settings.get("trail_giveback", 30.0)), ".0f") +
                            "% giveback, arms at +" + format(float(settings.get("trail_gate_r", 1.0)), ".2f") + "R)")
                st.caption("Best since entry: Rs " + fmt(text_) + " (" + tsrc + "). " + tp["reason"])
                if tp["improves"]:
                    if st.button("Apply stop Rs " + fmt(tp["suggested_stop"]), type="primary",
                                 key="mg_trail_apply", use_container_width=True):
                        engine.update_trade(int(mg_tid), stop=float(tp["suggested_stop"]))
                        note = ""
                        if lstate == "live" and mside != "short":
                            ok2, msg2 = sync_gtt_stop(kite_mcp_client(), msym, mqty, int(mg_tid),
                                                      float(tp["suggested_stop"]), mltp)
                            if not ok2:
                                note = " Kite side: " + msg2
                        elif mside == "short":
                            note = " Short protection is a MIS day order - re-place it above with the new trigger."
                        elif lstate != "live":
                            note = " Connect the live feed to also move a GTT stop on Kite."
                        st.session_state["trades_flash"] = ("Trail stop now Rs " + fmt(tp["suggested_stop"]) +
                                                            " in the journal." + note)
                        try:
                            st.session_state.pop("mg_stop", None)
                        except Exception:
                            pass
                        st.rerun()
            else:
                st.caption("Trail unavailable: " + str(tsrc))
            if lstate == "live":
                km_ = kite_mcp_client()
                try:
                    ggs = orders.active_gtts(km_, msym)
                    oos = orders.open_orders(km_, msym)
                except Exception as e:
                    st.warning("Could not read Kite protections: " + str(e))
                    ggs, oos = [], []
                bits = []
                for g in ggs:
                    bits.append("GTT #" + str(g.get("id")) + " [" +
                                ", ".join("trigger Rs " + fmt(l.get("trigger")) for l in orders.gtt_legs(g)) + "]")
                for o in oos:
                    bits.append(str(o.get("order_type")) + " " + str(o.get("transaction_type")) + " x" +
                                str(o.get("quantity")) +
                                ((" @ trigger Rs " + fmt(o.get("trigger_price"))) if o.get("trigger_price")
                                 else ((" @ Rs " + fmt(o.get("price"))) if o.get("price") else "")) +
                                " [" + str(o.get("status")) + "]")
                st.write("**On Kite for " + msym + ":** " + (" | ".join(bits) if bits else "no live protection orders"))
                p1, p2, p3 = st.columns(3)
                if mside != "short":
                    if p1.button("Place protective GTT (stop + target)", key="mg_gtt"):
                        try:
                            lpx = km_.ltp([msym]).get(msym) or mltp
                            if not lpx:
                                st.error("No live price - cannot place the GTT.")
                            else:
                                km_.place_gtt(tradingsymbol=msym, transaction_type="SELL", product="CNC",
                                              trigger_type="two-leg", last_price=float(lpx),
                                              upper_trigger_value=round(mtgt, 2),
                                              upper_limit_price=round(mtgt * 0.995, 2), upper_quantity=mqty,
                                              lower_trigger_value=round(mstop, 2),
                                              lower_limit_price=round(mstop * 0.995, 2), lower_quantity=mqty)
                                st.success("Protective GTT placed: sell stop Rs " + fmt(mstop) + " / target Rs " +
                                           fmt(mtgt) + " - Kite watches it even when this app is closed.")
                        except Exception as e:
                            st.error("GTT failed: " + str(e))
                    twoleg = [g for g in ggs if len(orders.gtt_legs(g)) >= 2]
                    if p2.button("Sync GTT stop to Rs " + fmt(nstop), key="mg_gsync", disabled=not twoleg):
                        ok2, msg2 = sync_gtt_stop(km_, msym, mqty, int(mg_tid), float(nstop), mltp)
                        if ok2:
                            st.success(msg2 + " The journal stop now matches.")
                        else:
                            st.error(msg2)
                else:
                    if p1.button("Place stop order (SL-M BUY x" + str(mqty) + ")", key="mg_sl"):
                        try:
                            km_.place_order(tradingsymbol=msym, transaction_type="BUY", quantity=mqty,
                                            product="MIS", order_type="SL-M", trigger_price=round(float(nstop), 2),
                                            tag="SQD-STOP")
                            st.success("Stop order placed: SL-M BUY trigger Rs " + fmt(nstop) +
                                       " (MIS = valid for today only).")
                        except Exception as e:
                            st.error("Stop order failed: " + str(e))
                    if p2.button("Place target order (LIMIT BUY)", key="mg_tp"):
                        try:
                            km_.place_order(tradingsymbol=msym, transaction_type="BUY", quantity=mqty,
                                            product="MIS", order_type="LIMIT", price=round(float(ntgt), 2),
                                            tag="SQD-TGT")
                            st.success("Target order placed: LIMIT BUY Rs " + fmt(ntgt) + " (MIS = valid for today only).")
                        except Exception as e:
                            st.error("Target order failed: " + str(e))
                st.divider()
                st.markdown("**Exit now (early exit)**")
                exit_now_block(int(mg_tid), msym, mside, mqty, mltp, lstate, key="mg_exit")
                st.divider()
            with st.form("close_form"):
                exit_px = st.number_input("Exit price (journal only)", min_value=0.01,
                                          value=float(mltp) if mltp else 0.01)
                if st.form_submit_button("Mark closed in journal (no order placed)"):
                    engine.close_trade(int(mg_tid), float(exit_px))
                    st.success("Trade " + str(mg_tid) + " closed in the journal.")
                    st.rerun()
    else:
        st.info("No open trades. Scan, log an entry from the Decision desk, and it will show up here with exit guidance.")

    st.divider()
    st.markdown("#### Orders placed from the app (Kite)")
    pending_rows = orders.load_pending()
    if not pending_rows:
        st.caption("Orders placed from the desk panels land here: live status, and fills auto-log to the journal "
                   "(entries) or close trades at the actual price (exits).")
    else:
        if st.button("Check orders / fills now", key="sync_btn"):
            try:
                msgs = orders.sync(kite_mcp_client())
                st.session_state["sync_msgs"] = msgs or ["Nothing new."]
                kite_state.clear()
                live_ltp.clear()
            except Exception as e:
                st.session_state["sync_msgs"] = ["Sync failed: " + str(e)]
        for m_ in st.session_state.pop("sync_msgs", []):
            st.write("- " + m_)
        st.dataframe(pd.DataFrame([{
            "Placed": rec.get("placed"), "Kind": rec.get("kind"), "Symbol": rec.get("symbol"),
            "Qty": rec.get("qty"), "Order id": rec.get("order_id"), "Status": rec.get("status"),
            "Avg price": rec.get("avg_price"), "Journal #": rec.get("journal_id"),
        } for rec in pending_rows]), use_container_width=True, hide_index=True)
        st.caption("Entries that fill are logged to the journal at the actual average price; exits that fill close "
                   "their trade. Rejected/cancelled orders are reported above - fix and re-place from the desk.")

    st.divider()
    st.markdown("#### Kite portfolio (read-only)")
    if kc is None:
        st.info("Kite not connected. See README 'Zerodha account connection' for the 5-minute setup.")
    else:
        if st.button("Pull portfolio from Kite"):
            try:
                from kiteconnect import exceptions as kexc
                try:
                    margins = kc.margins()
                    holdings = kc.holdings()
                    positions = kc.positions()
                    st.session_state["kite_pf"] = (margins, holdings, positions,
                                                   dt.datetime.now().strftime("%H:%M"))
                except kexc.TokenException:
                    st.error("Kite session expired (tokens last one day). Re-run: python zerodha/connect.py login")
                except kexc.PermissionException as e:
                    st.error("Kite permission error: " + str(e))
            except Exception as e:
                st.error(str(e))
        if "kite_pf" in st.session_state:
            margins, holdings, positions, at = st.session_state["kite_pf"]
            avail = margins.get("equity", {}).get("available", {}).get("cash")
            if avail is not None:
                st.write("Equity available: Rs " + format(avail, ",.0f") + " | pulled at " + at)
            hold = [h for h in holdings if h.get("quantity", 0)]
            if hold:
                st.markdown("**Holdings**")
                st.dataframe(pd.DataFrame([{
                    "Symbol": h["tradingsymbol"], "Qty": h["quantity"], "Avg": h["average_price"],
                    "Last": h["last_price"], "P&L": round(h.get("pnl") or 0, 0),
                    "Day %": round(h.get("day_change_percentage") or 0, 2),
                } for h in hold]), use_container_width=True, hide_index=True)
            net = [p for p in positions.get("net", []) if p.get("quantity", 0)]
            if net:
                st.markdown("**Open positions**")
                st.dataframe(pd.DataFrame([{
                    "Symbol": p["tradingsymbol"], "Product": p["product"], "Qty": p["quantity"],
                    "Avg": p["average_price"], "Last": p["last_price"],
                    "P&L": round(p.get("pnl") or 0, 0),
                } for p in net]), use_container_width=True, hide_index=True)
            if not hold and not net:
                st.write("No holdings or open positions in the account.")

with tab_journal:
    jdf = engine.load_journal()
    if len(jdf):
        closed = jdf[jdf["status"] == "closed"]
        open_j = jdf[jdf["status"] == "open"]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Trades", str(len(jdf)))
        c2.metric("Open", str(len(open_j)))
        if len(closed):
            wins = (pd.to_numeric(closed["pnl"], errors="coerce") > 0).sum()
            c3.metric("Win rate", format(wins / len(closed) * 100, ".0f") + "%")
            c4.metric("Total P&L", "Rs " + format(pd.to_numeric(closed["pnl"], errors="coerce").sum(), ",.0f"))
            c5.metric("Avg R", format(pd.to_numeric(closed["r_multiple"], errors="coerce").mean(), "+.2f"))
        if len(open_j):
            risk_sum = 0.0
            for _, rw in open_j.iterrows():
                e_, s_, q_ = float(rw["entry"]), float(rw["stop"]), float(rw["qty"])
                risk_sum += (e_ - s_) * q_ if str(rw.get("side") or "long") == "long" else (s_ - e_) * q_
            st.caption("Open risk (entry-to-stop across all open trades): Rs " + format(risk_sum, ",.0f") +
                       " = " + format(risk_sum / capital * 100, ".1f") + "% of capital")
        st.dataframe(jdf.sort_values("id", ascending=False), use_container_width=True, hide_index=True)
        st.download_button("Download journal CSV", jdf.to_csv(index=False).encode("utf-8"),
                           file_name="trades.csv", mime="text/csv")
    else:
        st.info("Journal is empty. Log entries from the Decision desk; exits from My Trades or the desk's fast actions.")

st.divider()
st.caption("Swing Desk - rule-based, educational assistance. Not SEBI-registered investment advice. "
           "Prices are delayed unless the live Kite feed is connected; orders from the app are confirm-gated; "
           "position sizing uses the sidebar settings.")
