#!/usr/bin/env python
"""Zerodha Kite Connect helper - daily login + read-only portfolio snapshot.

One-time setup:
  1. Create an app at https://developers.kite.trade
     (the 'Personal (Free)' plan is enough for account access; market data needs the paid plan)
  2. Copy zerodha/.env.example to zerodha/.env and paste your API key + secret
  3. python zerodha/connect.py login     -> prints login URL, log in on Zerodha's own page
  4. python zerodha/connect.py token <pasted redirect URL>   -> saves the session
  5. python zerodha/connect.py snapshot  -> portfolio report in reports/

Notes:
  - You log in on Zerodha's page yourself; this tool never sees your password or TOTP.
  - Access tokens expire daily (~7:30 AM IST): run login + token once each trading day.
  - Read-only: no order placement in this tool.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZDIR = os.path.join(ROOT, "zerodha")
ENV_PATH = os.path.join(ZDIR, ".env")
TOKEN_PATH = os.path.join(ZDIR, "token.json")
REPORTS_DIR = os.path.join(ROOT, "reports")


def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    for k in ("KITE_API_KEY", "KITE_API_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def load_token():
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            return None
    return None


def get_client():
    env = load_env()
    key = env.get("KITE_API_KEY")
    if not key:
        sys.exit("Missing KITE_API_KEY. Copy zerodha/.env.example to zerodha/.env and fill it in.")
    tok = load_token()
    if not tok or not tok.get("access_token"):
        sys.exit("No saved session. Run: python zerodha/connect.py login")
    if tok.get("date") != dt.date.today().isoformat():
        print("[warn] session is from " + str(tok.get("date")) + "; Kite tokens expire ~7:30 AM IST. If calls fail, log in again.")
    kite = KiteConnect(api_key=key)
    kite.set_access_token(tok["access_token"])
    return kite


def cmd_login():
    env = load_env()
    key = env.get("KITE_API_KEY")
    if not key:
        sys.exit("Missing KITE_API_KEY. Copy zerodha/.env.example to zerodha/.env and fill it in.")
    url = KiteConnect(api_key=key).login_url()
    print("Open this URL in your browser and log in with your Zerodha credentials:")
    print()
    print("  " + url)
    print()
    print("After login you land on your app's redirect URL (page may show an error - fine).")
    print("Copy the FULL address from the browser address bar, then run:")
    print()
    print("  python zerodha/connect.py token \"<paste the full redirect URL here>\"")
    print()
    print("(The URL contains a one-time request_token; it expires in a few minutes.)")


def cmd_token(raw):
    env = load_env()
    key, secret = env.get("KITE_API_KEY"), env.get("KITE_API_SECRET")
    if not key or not secret:
        sys.exit("Set KITE_API_KEY and KITE_API_SECRET in zerodha/.env first.")
    m = re.search(r"request_token=([A-Za-z0-9]+)", raw)
    request_token = m.group(1) if m else raw.strip()
    if not request_token:
        sys.exit("Could not find a request_token in the input.")
    kite = KiteConnect(api_key=key)
    try:
        session = kite.generate_session(request_token, api_secret=secret)
    except kite_exc.KiteException as e:
        sys.exit("Login failed: " + str(e))
    data = {
        "access_token": session["access_token"],
        "user_id": session.get("user_id"),
        "date": dt.date.today().isoformat(),
    }
    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print("Session saved for user " + str(data["user_id"]) + " (" + data["date"] + ").")
    print("Valid for today; tokens expire ~7:30 AM IST. Re-run login + token tomorrow.")


def cmd_status():
    kite = get_client()
    p = kite.profile()
    print("Connected: " + p.get("user_name", "?") + " (" + p.get("user_id", "?") + ") | " + p.get("email", ""))


def rs(v):
    return "Rs " + format(v, ",.2f")


def cmd_snapshot():
    kite = get_client()
    try:
        margins = kite.margins()
        holdings = kite.holdings()
        positions = kite.positions()
    except kite_exc.TokenException:
        sys.exit("Session expired/invalid. Run: python zerodha/connect.py login")
    except kite_exc.PermissionException as e:
        sys.exit("Permission denied: " + str(e) + "\nNote: quotes/market data need the paid Kite Connect plan; portfolio access is free.")

    eq = margins.get("equity", {})
    avail = eq.get("available", {}).get("cash")
    used = eq.get("utilised", {}).get("debit", 0)

    lines = ["# Portfolio snapshot - " + dt.date.today().isoformat(), ""]
    lines.append("Kite Connect, pulled at " + dt.datetime.now().strftime("%Y-%m-%d %H:%M") + ".")
    lines.append("")
    if avail is not None:
        lines.append("## Margins (equity)")
        lines.append("- Available cash: " + rs(avail))
        lines.append("- Utilised: " + rs(used or 0))
        lines.append("")

    lines.append("## Holdings (delivery)")
    hold = [h for h in holdings if h.get("quantity", 0) != 0]
    total_val = total_pnl = 0.0
    if hold:
        lines.append("| Symbol | Qty | Avg cost | Last | Value | P&L | P&L % | Day % |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for h in sorted(hold, key=lambda x: x.get("pnl") or 0, reverse=True):
            qty = h.get("quantity", 0)
            avg = h.get("average_price", 0)
            last = h.get("last_price", 0)
            val = qty * last
            pnl = h.get("pnl", 0) or 0
            pnl_pct = (pnl / (qty * avg) * 100) if qty and avg else 0
            total_val += val
            total_pnl += pnl
            lines.append("| " + " | ".join([
                h.get("tradingsymbol", "?"),
                str(qty),
                format(avg, ",.2f"),
                format(last, ",.2f"),
                format(val, ",.0f"),
                format(pnl, "+,.0f"),
                format(pnl_pct, "+.1f") + "%",
                format(h.get("day_change_percentage", 0) or 0, "+.2f") + "%",
            ]) + " |")
        tot_pct = (total_pnl / (total_val - total_pnl) * 100) if (total_val - total_pnl) else 0
        lines.append("")
        lines.append("**Total holdings value " + rs(total_val) + " | P&L " + format(total_pnl, "+,.2f") + " (" + format(tot_pct, "+.1f") + "%)**")
    else:
        lines.append("No holdings.")
    lines.append("")

    net = [p for p in positions.get("net", []) if p.get("quantity", 0) != 0]
    lines.append("## Open positions (net)")
    if net:
        lines.append("| Symbol | Product | Qty | Avg | Last | P&L |")
        lines.append("|---|---|---|---|---|---|")
        for p in net:
            lines.append("| " + " | ".join([
                p.get("tradingsymbol", "?"),
                p.get("product", ""),
                str(p.get("quantity", 0)),
                format(p.get("average_price", 0), ",.2f"),
                format(p.get("last_price", 0), ",.2f"),
                format(p.get("pnl", 0) or 0, "+,.0f"),
            ]) + " |")
    else:
        lines.append("No open positions.")
    lines.append("")
    lines.append("*Read-only snapshot via Kite Connect. Verify in the Kite app before acting.*")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, "portfolio_" + dt.date.today().isoformat() + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    if avail is not None:
        print("Equity available: " + rs(avail))
    print("Holdings: " + str(len(hold)) + " | total value " + rs(total_val) + " | P&L " + format(total_pnl, "+,.2f"))
    if net:
        print("Open positions: " + str(len(net)))
    print("Report: " + path)


def main():
    ap = argparse.ArgumentParser(description="Zerodha Kite Connect helper (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="print the login URL")
    tok = sub.add_parser("token", help="exchange the pasted redirect URL for a session")
    tok.add_argument("redirect", nargs="?", help="full redirect URL (or just the request_token)")
    sub.add_parser("status", help="check the saved session")
    sub.add_parser("snapshot", help="pull margins/holdings/positions into a report")
    args = ap.parse_args()

    if args.cmd == "login":
        cmd_login()
    elif args.cmd == "token":
        raw = args.redirect
        if not raw and sys.stdin.isatty():
            raw = input("Paste the full redirect URL: ").strip()
        if not raw:
            sys.exit("Give the redirect URL: python zerodha/connect.py token \"<url>\"")
        cmd_token(raw)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "snapshot":
        cmd_snapshot()


if __name__ == "__main__":
    main()
