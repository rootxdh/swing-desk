"""Live Kite feed + order passthrough via Zerodha's official Kite MCP server.

The app acts as its own MCP client to https://mcp.kite.trade/mcp: one session per
process, authorised once per session by the user clicking a Zerodha-hosted login
link (the app never sees the Zerodha password/TOTP). Read calls (get_ltp,
get_quotes, get_profile) and order calls (place/modify/cancel orders, GTTs) go
through the same session.

Why hand-rolled on `requests` instead of the official `mcp` SDK: the SDK requires
Python >= 3.10 and this machine runs the app on Python 3.9. The server answers the
streamable-HTTP transport with plain JSON (no SSE) for this workflow, which is
simple to speak: initialize -> notifications/initialized -> tools/call, with the
session id carried in the Mcp-Session-Id header. The session id is persisted to
data/kite_mcp_session.json so app reloads can try to resume it (the user then
skips re-login unless the session or Kite token expired).

Read-only usage of quotes can fail with NotLoggedInError - catch it and show the
link from login_url().
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from swing import store

MCP_URL = "https://mcp.kite.trade/mcp"
PROTOCOL = "2025-03-26"  # version negotiated by the hosted server
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SESSION_FILE = os.path.join(DATA_DIR, "kite_mcp_session.json")

INDEX_ALIASES = {
    "NIFTY": "NSE:NIFTY 50", "NIFTY50": "NSE:NIFTY 50", "NIFTY 50": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK", "NIFTY BANK": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE", "NIFTY FIN SERVICE": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX",
    "INDIA VIX": "NSE:INDIA VIX", "VIX": "NSE:INDIA VIX",
}


class KiteMCPError(Exception):
    """Error returned by (or while talking to) the Kite MCP server."""


class NotLoggedInError(KiteMCPError):
    """The MCP session is up but not authorised yet - show the login link."""


def kite_symbol(symbol):
    s = str(symbol).strip().upper()
    if ":" in s:
        return s
    return INDEX_ALIASES.get(s, "NSE:" + s)


def market_open_now():
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    return (9, 15) <= (ist.hour, ist.minute) <= (15, 30)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _candle_date(x):
    """Candle timestamp (epoch seconds/ms or date string) -> 'YYYY-MM-DD' (IST)."""
    if isinstance(x, (int, float)):
        ts = float(x)
        if ts > 1e11:
            ts /= 1000.0
        ist = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=5, minutes=30)
        return ist.strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(x))
    return m.group(1) if m else None


class KiteMCP:
    def __init__(self, url=MCP_URL, timeout=30):
        self.url = url
        self.timeout = timeout
        self._http = requests.Session()
        self._lock = threading.RLock()
        self._rid = 0
        self._sid = None
        self._started = False

    @property
    def session_id(self):
        return self._sid

    def _next_id(self):
        self._rid += 1
        return self._rid

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL}
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        return h

    def _post(self, payload, allow_empty=False):
        r = self._http.post(self.url, headers=self._headers(), json=payload, timeout=self.timeout)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._sid = sid
        if r.status_code in (202, 204) or not r.text.strip():
            if allow_empty:
                return None
            raise KiteMCPError("Empty reply from Kite MCP (HTTP %s)" % r.status_code)
        if r.status_code >= 400:
            raise KiteMCPError("Kite MCP HTTP %s: %s" % (r.status_code, r.text[:300]))
        body = r.text
        if "text/event-stream" in (r.headers.get("content-type") or ""):
            body = self._first_sse_data(body)
        try:
            return json.loads(body)
        except ValueError:
            raise KiteMCPError("Unparsable Kite MCP reply: " + body[:300])

    @staticmethod
    def _first_sse_data(text):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return line[5:].strip()
        raise KiteMCPError("No data event in Kite MCP SSE reply")

    def _rpc(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._next_id()
        try:
            msg = self._post(payload, allow_empty=notify)
        except requests.RequestException as e:
            raise KiteMCPError("Kite MCP network error: " + str(e))
        if notify:
            return None
        if msg is None:
            raise KiteMCPError("Kite MCP gave no reply")
        if "error" in msg:
            raise KiteMCPError(str((msg["error"] or {}).get("message") or msg["error"]))
        return msg.get("result")

    def _tool_call(self, name, arguments):
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if res is None:
            raise KiteMCPError("Empty tool result for " + name)
        texts = [c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        if res.get("isError"):
            if "log in" in text.lower():
                raise NotLoggedInError(text or "Please log in first")
            raise KiteMCPError(text or ("Tool error: " + name))
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    # ---- session -------------------------------------------------------

    def start(self, session_id=None):
        with self._lock:
            if self._started and self._sid:
                return
            if session_id is None:
                session_id = self._load_saved()
            if session_id:
                self._sid = str(session_id)
                try:
                    self._tool_call("get_profile", {})
                    self._started = True
                    return
                except NotLoggedInError:
                    self._started = True
                    return
                except Exception:
                    self._sid = None
            self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "stocks-qoder", "version": "1.0"}}})
            if not self._sid:
                raise KiteMCPError("Kite MCP did not return a session id")
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, allow_empty=True)
            self._started = True
            self._save_saved()

    def _load_saved(self):
        try:
            if store.have_db():
                return (store.kv_get("kite_mcp_session", {}) or {}).get("session_id")
            with open(SESSION_FILE, encoding="utf-8") as fh:
                return json.load(fh).get("session_id")
        except Exception:
            return None

    def _save_saved(self):
        try:
            if store.have_db():
                store.kv_set("kite_mcp_session",
                             {"session_id": self._sid, "saved_at": datetime.now().isoformat()})
                return
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SESSION_FILE, "w", encoding="utf-8") as fh:
                json.dump({"session_id": self._sid, "saved_at": datetime.now().isoformat()}, fh)
        except Exception:
            pass

    def call(self, name, arguments=None, retry=True):
        """Run a tool call. retry=False for order writes - a write must never be
        re-sent automatically (duplicate-order risk); only a session-level
        rejection (a request that never executed: HTTP 4xx, or the server's
        'Failed to execute' for a reaped session) justifies a rebuild + resend."""
        with self._lock:
            if not self._started:
                self.start()
            try:
                return self._tool_call(name, arguments)
            except NotLoggedInError:
                raise
            except KiteMCPError as e:
                msg = str(e)
                if not retry or ("HTTP 4" not in msg and "Failed to execute" not in msg):
                    raise
                self._started = False
                self._sid = None
                self.start()
                return self._tool_call(name, arguments)

    def start_keepalive(self, interval=20):
        """Daemon thread that pings the session so the server does not reap it,
        and eagerly rebuilds when a ping fails (a fresh session + fresh login
        link is then shown by the UI instead of a dead one)."""
        if getattr(self, "_ka", None) is not None and self._ka.is_alive():
            return

        def loop():
            while True:
                time.sleep(interval)
                try:
                    with self._lock:
                        if not self._started:
                            self.start()
                        self._tool_call("get_profile", {})
                except NotLoggedInError:
                    pass  # alive, just not authorised yet
                except Exception:
                    try:
                        with self._lock:
                            self._started = False
                            self._sid = None
                            self.start()
                    except Exception:
                        pass

        self._ka = threading.Thread(target=loop, daemon=True, name="kite-mcp-keepalive")
        self._ka.start()

    # ---- read helpers --------------------------------------------------

    def status(self):
        """('live', user) | ('login', reason) | ('error', reason)."""
        try:
            pf = self.call("get_profile")
            name = pf.get("user_name") or pf.get("user_id") or "connected"
            return "live", str(name)
        except NotLoggedInError:
            return "login", "needs Zerodha login"
        except Exception as e:
            return "error", str(e)

    def login_url(self):
        raw = self.call("login")
        text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
        m = re.search(r"https://mcp\.kite\.trade/authorize\S+", text)
        if not m:
            raise KiteMCPError("No login URL in reply: " + text[:200])
        return m.group(0).rstrip(".,)")

    def ltp(self, symbols):
        """{original_symbol: last_price or None} - one batched get_ltp call."""
        pairs = [(str(s).strip(), kite_symbol(s)) for s in symbols]
        keys = list(dict.fromkeys(k for _, k in pairs))
        data = self.call("get_ltp", {"instruments": keys}) if keys else {}
        out = {}
        for orig, key in pairs:
            item = (data or {}).get(key) or {}
            out[orig] = item.get("last_price")
        return out

    def quotes(self, symbols):
        """{original_symbol: full quote dict or None} - OHLC, volume, depth."""
        pairs = [(str(s).strip(), kite_symbol(s)) for s in symbols]
        keys = list(dict.fromkeys(k for _, k in pairs))
        data = self.call("get_quotes", {"instruments": keys}) if keys else {}
        out = {}
        for orig, key in pairs:
            out[orig] = (data or {}).get(key)
        return out

    def profile(self):
        return self.call("get_profile")

    def historical_daily(self, symbol, days=120):
        """Daily candles, oldest first: [{date, open, high, low, close, volume}].
        Resolves the instrument token via search_instruments, then get_historical_data."""
        key = kite_symbol(symbol)
        found = self.call("search_instruments", {"query": key, "filter_on": "id", "limit": 5})
        rows = (found or {}).get("data") if isinstance(found, dict) else None
        rows = rows or []
        row = next((r for r in rows if str(r.get("id", "")).upper() == key),
                   rows[0] if rows else None)
        if not row or not row.get("instrument_token"):
            raise KiteMCPError("No instrument token found for " + key)
        now = datetime.now(timezone.utc)
        stamp = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
        raw = self.call("get_historical_data", {
            "instrument_token": int(row["instrument_token"]),
            "from_date": stamp(now - timedelta(days=int(days))),
            "to_date": stamp(now), "interval": "day"})
        candles = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(candles, list):
            candles = []
        out = []
        for c in candles:
            if isinstance(c, dict):
                d = c.get("date", c.get("timestamp", c.get("time")))
                o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
                v = c.get("volume")
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                d, o, h, l, cl = c[0], c[1], c[2], c[3], c[4]
                v = c[5] if len(c) > 5 else None
            else:
                continue
            ds = _candle_date(d)
            if ds is None or _num(h) is None or _num(l) is None:
                continue
            out.append({"date": ds, "open": _num(o), "high": _num(h),
                        "low": _num(l), "close": _num(cl), "volume": _num(v)})
        out.sort(key=lambda r: r["date"])
        return out

    # ---- orders (thin passthroughs; Kite validates everything) ---------

    def place_order(self, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, trigger_price=None, exchange="NSE", variety="regular",
                    validity="DAY", tag=None):
        args = {"variety": variety, "exchange": exchange, "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type, "quantity": int(quantity),
                "product": product, "order_type": order_type}
        if price is not None:
            args["price"] = float(price)
        if trigger_price is not None:
            args["trigger_price"] = float(trigger_price)
        if validity:
            args["validity"] = validity
        if tag:
            args["tag"] = str(tag)[:20]
        return self.call("place_order", args, retry=False)

    def modify_order(self, order_id, order_type, quantity=None, price=None,
                     trigger_price=None, variety="regular", validity=None):
        args = {"variety": variety, "order_id": str(order_id), "order_type": order_type}
        if quantity is not None:
            args["quantity"] = int(quantity)
        if price is not None:
            args["price"] = float(price)
        if trigger_price is not None:
            args["trigger_price"] = float(trigger_price)
        if validity:
            args["validity"] = validity
        return self.call("modify_order", args, retry=False)

    def cancel_order(self, order_id, variety="regular"):
        return self.call("cancel_order", {"variety": variety, "order_id": str(order_id)}, retry=False)

    def orders(self):
        return self.call("get_orders")

    def order_history(self, order_id):
        return self.call("get_order_history", {"order_id": str(order_id)})

    def order_trades(self, order_id):
        return self.call("get_order_trades", {"order_id": str(order_id)})

    def gtts(self):
        return self.call("get_gtts")

    def place_gtt(self, tradingsymbol, transaction_type, product, trigger_type,
                  last_price, trigger_value=None, limit_price=None, quantity=None,
                  upper_trigger_value=None, upper_limit_price=None, upper_quantity=None,
                  lower_trigger_value=None, lower_limit_price=None, lower_quantity=None,
                  exchange="NSE"):
        args = {"exchange": exchange, "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type, "product": product,
                "trigger_type": trigger_type, "last_price": float(last_price)}
        for k, v in (("trigger_value", trigger_value), ("limit_price", limit_price),
                     ("quantity", quantity), ("upper_trigger_value", upper_trigger_value),
                     ("upper_limit_price", upper_limit_price), ("upper_quantity", upper_quantity),
                     ("lower_trigger_value", lower_trigger_value),
                     ("lower_limit_price", lower_limit_price), ("lower_quantity", lower_quantity)):
            if v is not None:
                args[k] = float(v) if k != "quantity" and "quantity" not in k else int(v)
        return self.call("place_gtt_order", args, retry=False)

    def modify_gtt(self, trigger_id, tradingsymbol, transaction_type, product, trigger_type,
                   last_price, **legs):
        args = {"trigger_id": int(trigger_id), "exchange": "NSE", "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type, "product": product,
                "trigger_type": trigger_type, "last_price": float(last_price)}
        args.update({k: v for k, v in legs.items() if v is not None})
        return self.call("modify_gtt_order", args, retry=False)

    def delete_gtt(self, trigger_id):
        return self.call("delete_gtt_order", {"trigger_id": int(trigger_id)}, retry=False)


def _cli():
    import sys
    sys.stdout.reconfigure(errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    k = KiteMCP()
    if cmd == "probe":
        k.start()
        state, detail = k.status()
        print("session:", k._sid)
        print("status:", state, "|", detail)
        if state == "login":
            print("login link:", k.login_url())
    elif cmd == "login":
        print("WARNING: AI systems are unpredictable and non-deterministic. "
              "By continuing, you agree to interact with your Zerodha account via AI at your own risk.")
        print(k.login_url())
        for _ in range(50):
            state, detail = k.status()
            if state == "live":
                print("AUTHORIZED as", detail)
                break
            time.sleep(3)
        else:
            print("timed out waiting for login (session:", k._sid, ")")
    elif cmd == "ltp":
        syms = sys.argv[2:] or ["RELIANCE", "TCS", "NIFTY"]
        print(json.dumps(k.ltp(syms), indent=1))
    elif cmd == "status":
        print(k.status(), "| session:", k._sid)
    else:
        print("usage: python swing/kite_mcp.py [probe|login|ltp SYM...|status]")


if __name__ == "__main__":
    _cli()
