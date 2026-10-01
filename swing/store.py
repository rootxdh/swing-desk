"""One place that knows where the app's data lives.

Two backends, picked automatically:

* Files (default) - journal/trades.csv, settings.json, data/*.json. Zero setup
  for a single machine; this is how the app has always worked.
* Postgres - used when DATABASE_URL is set (Neon in the cloud). The journal
  becomes a `trades` table; everything small (settings, pending orders, armed
  plans, the Kite MCP session) becomes JSONB documents in `app_state`. This
  lets a free host with an ephemeral disk (Streamlit Community Cloud, Render
  free) run the app, and lets the local copy and the hosted copy share one
  source of truth.

DATABASE_URL is read from the environment; locally it can also live in
zerodha/.env (never committed) as:  DATABASE_URL=postgresql://...?sslmode=require

CLI:
  python -m swing.store status    - show backend + row counts
  python -m swing.store migrate   - copy local files into Postgres (idempotent)
"""
import argparse
import json
import os
import threading
from urllib.parse import urlparse

import numpy as np
import pandas as pd

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except Exception:  # file mode works without the Postgres driver installed
    psycopg2 = None
    Json = execute_values = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
JOURNAL_CSV = os.path.join(ROOT, "journal", "trades.csv")
SETTINGS_JSON = os.path.join(ROOT, "settings.json")
ENV_FILE = os.path.join(ROOT, "zerodha", ".env")
SESSION_JSON = os.path.join(DATA_DIR, "kite_mcp_session.json")
PENDING_JSON = os.path.join(DATA_DIR, "pending_orders.json")

TRADE_COLS = ["id", "status", "date_in", "symbol", "side", "qty", "entry", "stop", "target",
              "setup", "quality", "notes", "date_out", "exit_price", "pnl", "r_multiple"]

lock = threading.RLock()
_ready_urls = set()


class StoreError(Exception):
    """Storage layer problem (missing driver, unreachable database)."""


def db_url():
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url and os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return url or None


def have_db():
    return db_url() is not None


def _connect():
    url = db_url()
    if not url:
        raise StoreError("DATABASE_URL is not set - the app is in file mode.")
    if psycopg2 is None:
        raise StoreError("DATABASE_URL is set but psycopg2 is missing (pip install psycopg2-binary).")
    return psycopg2.connect(url, connect_timeout=15)


def _with_conn(fn):
    with lock:
        conn = _connect()
        try:
            cur = conn.cursor()
            url = db_url()
            if url not in _ready_urls:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS trades ("
                    "id INTEGER PRIMARY KEY, status TEXT, date_in TEXT, symbol TEXT, side TEXT,"
                    "qty INTEGER, entry DOUBLE PRECISION, stop DOUBLE PRECISION, target DOUBLE PRECISION,"
                    "setup TEXT, quality TEXT, notes TEXT, date_out TEXT, exit_price DOUBLE PRECISION,"
                    "pnl DOUBLE PRECISION, r_multiple DOUBLE PRECISION)")
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS app_state ("
                    "key TEXT PRIMARY KEY, value JSONB NOT NULL,"
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                _ready_urls.add(url)
            out = fn(cur)
            conn.commit()
            return out
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


def kv_get(key, default=None):
    def run(cur):
        cur.execute("SELECT value FROM app_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return default if row is None else row[0]
    return _with_conn(run)


def kv_set(key, value):
    def run(cur):
        cur.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Json(value, dumps=lambda v: json.dumps(v, default=str))))
    _with_conn(run)


def _clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def load_trades_df():
    def run(cur):
        cur.execute("SELECT " + ", ".join(TRADE_COLS) + " FROM trades ORDER BY id")
        return pd.DataFrame(cur.fetchall(), columns=TRADE_COLS)
    return _with_conn(run)


def save_trades_df(df):
    out = df.copy()
    for c in TRADE_COLS:
        if c not in out.columns:
            out[c] = None
    records = [tuple(_clean(v) for v in rec)
               for rec in out[TRADE_COLS].itertuples(index=False, name=None)]

    def run(cur):
        cur.execute("DELETE FROM trades")
        if records:
            execute_values(cur, "INSERT INTO trades (" + ", ".join(TRADE_COLS) + ") VALUES %s",
                           records)
    _with_conn(run)


def migrate_from_files():
    """Copy the local file stores into Postgres. Safe to re-run - last write wins."""
    if not have_db():
        raise StoreError("DATABASE_URL is not set - nothing to migrate into.")
    lines = []
    if os.path.exists(JOURNAL_CSV):
        try:
            df = pd.read_csv(JOURNAL_CSV)
        except Exception:
            df = pd.DataFrame(columns=TRADE_COLS)
        save_trades_df(df)
        lines.append("journal: " + str(len(df)) + " trade(s) -> trades table")
    else:
        lines.append("journal: no trades.csv found - skipped")
    for key, path in (("settings", SETTINGS_JSON),
                      ("pending_orders", PENDING_JSON),
                      ("kite_mcp_session", SESSION_JSON)):
        if not os.path.exists(path):
            lines.append(key + ": no file - skipped")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            lines.append(key + ": unreadable (" + str(e) + ") - skipped")
            continue
        kv_set(key, data)
        lines.append(key + ": copied")
    return lines


def status():
    url = db_url()
    if not url:
        return ["backend: files (DATABASE_URL not set)"]
    parsed = urlparse(url)
    out = ["backend: postgres @ " + str(parsed.hostname or "?"),
           "database: " + str(parsed.path or "").lstrip("/")]
    try:
        def run(cur):
            cur.execute("SELECT count(*) FROM trades")
            n = cur.fetchone()[0]
            cur.execute("SELECT key, updated_at FROM app_state ORDER BY key")
            return n, cur.fetchall()
        n, keys = _with_conn(run)
        out.append("trades: " + str(n) + " row(s)")
        out.append("app_state keys: " + (", ".join(k + " (" + str(t)[:16] + ")" for k, t in keys)
                                         if keys else "none"))
    except Exception as e:
        out.append("connection failed: " + str(e))
    return out


def main():
    ap = argparse.ArgumentParser(description="Swing Desk storage: status / file->Postgres migration.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show backend and row counts")
    sub.add_parser("migrate", help="copy local files into Postgres (idempotent)")
    args = ap.parse_args()
    if args.cmd == "status":
        for line in status():
            print(line)
    elif args.cmd == "migrate":
        for line in migrate_from_files():
            print(line)
        print("done.")


if __name__ == "__main__":
    main()
