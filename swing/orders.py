"""Order workflow for the Swing Desk: remembers what the app placed in Kite,
syncs fills into the journal, and reads live protections (GTTs / stop orders).

Records live in data/pending_orders.json - one dict per placed order:
  {kind: entry|exit, order_id, symbol, side, qty, ...meta, status, avg_price,
   filled, journal_id, closed}

The Kite order book returned by MCP only shows the current day, so records that
are still open from a previous day are marked stale rather than silently trusted.
"""
import datetime as dt
import json
import os

from swing import engine, kite_mcp, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING_PATH = os.path.join(ROOT, "data", "pending_orders.json")

LIVE_STATUSES = ("OPEN", "TRIGGER PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
                 "MODIFY VALIDATION PENDING", "MODIFY PENDING")


def load_pending():
    if store.have_db():
        data = store.kv_get("pending_orders", [])
        return data if isinstance(data, list) else []
    try:
        with open(PENDING_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pending(rows):
    if store.have_db():
        store.kv_set("pending_orders", rows)
        return
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, default=str)


def record(kind, order_id, **meta):
    rows = load_pending()
    row = {"kind": kind, "order_id": str(order_id), "status": "PLACED",
           "placed": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    row.update(meta)
    rows.append(row)
    save_pending(rows)
    return row


def sync(km):
    """Pull today's Kite order book, update records, log fills to the journal.
    Returns a list of human-readable updates."""
    raw = km.orders()
    if isinstance(raw, dict):
        raw = raw.get("orders") or raw.get("data") or []
    by_id = {str(o.get("order_id")): o for o in raw if isinstance(o, dict)}
    rows = load_pending()
    msgs = []
    today = dt.date.today().isoformat()
    for row in rows:
        oid = str(row.get("order_id"))
        o = by_id.get(oid)
        sym = row.get("symbol", "?")
        if o is None:
            if row.get("status") in ("PLACED",) + LIVE_STATUSES and str(row.get("placed", ""))[:10] != today:
                row["status"] = "STALE"
                msgs.append(sym + " " + row["kind"] + " " + oid + ": no longer in today's order book "
                            "(expired or cancelled outside the app)")
            continue
        status = str(o.get("status") or "").upper()
        avg = o.get("average_price")
        filled = o.get("filled_quantity")
        if status:
            row["status"] = status
        if avg:
            row["avg_price"] = avg
        if filled is not None:
            row["filled"] = filled
        if row["kind"] == "entry" and status == "COMPLETE" and not row.get("journal_id"):
            qty = int(filled or o.get("quantity") or row.get("qty") or 0)
            px = float(avg or o.get("price") or row.get("planned_entry") or 0)
            if qty and px:
                tid = engine.add_trade(sym, qty, round(px, 2), float(row.get("stop") or 0),
                                       float(row.get("target") or 0), str(row.get("setup") or ""),
                                       str(row.get("quality") or ""), "Kite order " + oid,
                                       side=row.get("side", "long"))
                row["journal_id"] = tid
                msgs.append(sym + ": entry FILLED at Rs " + format(px, ",.2f") + " - logged as trade #" +
                            str(tid) + ". Place the protective stop/GTT in My Trades.")
        elif row["kind"] == "exit" and status == "COMPLETE" and not row.get("closed"):
            px = float(avg or 0)
            if px and row.get("trade_id"):
                engine.close_trade(int(row["trade_id"]), round(px, 2))
                row["closed"] = True
                msgs.append(sym + ": exit FILLED at Rs " + format(px, ",.2f") + " - trade #" +
                            str(row["trade_id"]) + " closed in the journal")
        elif status in ("REJECTED", "CANCELLED"):
            msgs.append(sym + " " + row["kind"] + " " + oid + ": " + status +
                        ((" - " + str(o.get("status_message"))) if o.get("status_message") else ""))
        elif status in LIVE_STATUSES:
            msgs.append(sym + " " + row["kind"] + " " + oid + ": " + status + " - not filled yet")
    save_pending(rows)
    return msgs


def extract_gtts(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("gtt_orders", "data", "orders"):
            if isinstance(raw.get(key), list):
                return raw[key]
        for v in raw.values():
            if isinstance(v, list):
                return v
    return []


def active_gtts(km, symbol=None):
    out = []
    for g in extract_gtts(km.gtts()):
        if not isinstance(g, dict):
            continue
        cond = g.get("condition") or {}
        ts = str(cond.get("tradingsymbol") or g.get("tradingsymbol") or "")
        if symbol and ts.upper() != str(symbol).strip().upper():
            continue
        if str(g.get("status", "active")).lower() != "active":
            continue
        out.append(g)
    return out


def gtt_legs(g):
    cond = g.get("condition") or {}
    triggers = cond.get("trigger_values") or []
    olist = g.get("orders") or []
    legs = []
    for i, tv in enumerate(triggers):
        o = olist[i] if i < len(olist) else {}
        legs.append({"trigger": tv, "limit": o.get("price"), "qty": o.get("quantity"),
                     "type": o.get("transaction_type")})
    return legs


def open_orders(km, symbol=None):
    raw = km.orders()
    if isinstance(raw, dict):
        raw = raw.get("orders") or raw.get("data") or []
    out = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        if symbol and str(o.get("tradingsymbol", "")).upper() != str(symbol).strip().upper():
            continue
        if str(o.get("status", "")).upper() in LIVE_STATUSES:
            out.append(o)
    return out


def exit_spec(side, last_price, market_open=None):
    """(order_type, price) that will actually fill: MARKET while the session is
    open, else a LIMIT pushed through the last price to be first in the queue."""
    market_open = kite_mcp.market_open_now() if market_open is None else market_open
    last_price = float(last_price)
    if market_open:
        return "MARKET", None
    if str(side).lower() == "short":
        return "LIMIT", round(last_price * 1.004, 2)
    return "LIMIT", round(last_price * 0.996, 2)
