#!/usr/bin/env python
"""Trend analysis CLI for NSE stocks: short-term and long-term signals + markdown report.

Usage:
    python analyze.py RELIANCE TCS INFY
    python analyze.py --watchlist watchlists/default.txt --period 5y
    python analyze.py RELIANCE --fundamentals --refresh
"""
import argparse
import datetime as dt
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf

from swing import engine

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(ROOT, "reports")
CHARTS_DIR = os.path.join(REPORTS_DIR, "charts")


def make_chart(ym, d, path):
    w = d.tail(756)
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(w.index, w["Close"], label="Close", lw=1.4, color="#1f77b4")
    style = {"SMA20": ("#ff7f0e", "20-DMA"), "SMA50": ("#2ca02c", "50-DMA"), "SMA200": ("#d62728", "200-DMA")}
    for col, (color, lab) in style.items():
        ax1.plot(w.index, w[col], label=lab, lw=1.0, color=color)
    ax1.set_title(engine.display_name(ym) + " - daily closes, ~3 years")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(alpha=0.25)

    up = w["Close"] >= w["Close"].shift()
    ax2.bar(w.index, w["Volume"], color=np.where(up, "#2ca02c", "#d62728"), width=1.0, alpha=0.6)
    ax2.plot(w.index, w["VOL50"], color="#444444", lw=1.0, label="50-day avg vol")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def fundamentals(ym, close):
    try:
        t = yf.Ticker(ym)
        info = t.info
    except Exception:
        return {}
    out = []

    def add(label_, value, fmt):
        if value is None:
            return
        try:
            out.append((label_, fmt(float(value))))
        except (TypeError, ValueError):
            pass

    add("Market cap", info.get("marketCap"), lambda v: "Rs " + format(v / 1e7, ",.0f") + " Cr")
    add("P/E (TTM)", info.get("trailingPE"), lambda v: format(v, ".1f"))
    add("P/B", info.get("priceToBook"), lambda v: format(v, ".1f"))
    add("ROE", info.get("returnOnEquity"), lambda v: format(v * 100, ".1f") + "%")
    d2e = info.get("debtToEquity")
    if d2e is not None and d2e > 10:
        d2e = d2e / 100
    add("Debt / Equity", d2e, lambda v: format(v, ".2f"))
    add("Operating margin", info.get("operatingMargins"), lambda v: format(v * 100, ".1f") + "%")
    add("Net margin", info.get("profitMargins"), lambda v: format(v * 100, ".1f") + "%")
    add("Revenue growth (yoy)", info.get("revenueGrowth"), lambda v: format(v * 100, ".1f") + "%")
    add("Earnings growth (yoy)", info.get("earningsGrowth"), lambda v: format(v * 100, ".1f") + "%")
    try:
        divs = t.dividends
        if divs is not None and len(divs):
            cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.DateOffset(years=1)
            dy = divs[divs.index >= cutoff].sum() / close * 100
            out.append(("Dividend yield (ttm, computed)", format(dy, ".2f") + "%"))
    except Exception:
        pass
    return out


def money(v):
    return "Rs " + format(v, ",.2f")


def build_report(results, generated):
    lines = ["# Trend report - " + generated, ""]
    lines.append("Watchlist scan of " + str(len(results)) + " symbols. Signals are rule-based (moving averages, RSI, MACD, 52-week position, CAGR) - educational analysis, not investment advice.")
    lines.append("")
    lines.append("| Symbol | CMP | 1M % | 6M % | 1Y % | RSI | vs 20-DMA | vs 200-DMA | Short-term | Long-term |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    def f2(v):
        return "-" if v is None else format(v, "+.1f")

    for r in results:
        vs20 = (r["close"] / r["sma20"] - 1) * 100 if r["sma20"] else None
        vs200 = (r["close"] / r["sma200"] - 1) * 100 if r["sma200"] else None
        lines.append("| " + " | ".join([
            "**" + r["symbol"] + "**",
            money(r["close"]),
            f2(r["r1m"]),
            f2(r["r6m"]),
            f2(r["r1y"]),
            "-" if r["rsi"] is None else format(r["rsi"], ".1f"),
            f2(vs20) + "%" if vs20 is not None else "-",
            f2(vs200) + "%" if vs200 is not None else "-",
            r["st_label"] + " (" + format(r["st_score"], "+d") + "/" + str(r["st_n"]) + ")",
            r["lt_label"] + " (" + format(r["lt_score"], "+d") + "/" + str(r["lt_n"]) + ")",
        ]) + " |")

    for r in results:
        lines += ["", "## " + r["symbol"] + " - " + money(r["close"]) + " (" + r["date"] + ")", ""]
        lines.append("**Short-term read: " + r["st_label"] + "** | **Long-term read: " + r["lt_label"] + "**")
        lines.append("")
        parts = []
        if r["sma20"]:
            parts.append("20-DMA " + money(r["sma20"]) + " (" + format((r["close"] / r["sma20"] - 1) * 100, "+.1f") + "%)")
        if r["sma50"]:
            parts.append("50-DMA " + money(r["sma50"]) + " (" + format((r["close"] / r["sma50"] - 1) * 100, "+.1f") + "%)")
        if r["sma200"]:
            parts.append("200-DMA " + money(r["sma200"]) + " (" + format((r["close"] / r["sma200"] - 1) * 100, "+.1f") + "%)")
        if parts:
            lines.append("- Price vs averages: " + " | ".join(parts))
        mom = []
        if r["rsi"] is not None:
            mom.append("RSI(14) " + format(r["rsi"], ".1f"))
        if r["hist"] is not None:
            mom.append("MACD histogram " + format(r["hist"], "+.2f"))
        if mom:
            lines.append("- Momentum: " + " | ".join(mom))
        lines.append("- 52-week range: " + money(r["lo52"]) + " - " + money(r["hi52"]) +
                     (" (at " + format(r["pos52"], ".0f") + "% of range)" if r["pos52"] is not None else ""))
        lines.append("- Returns: 1W " + f2(r["r1w"]) + "% | 1M " + f2(r["r1m"]) + "% | 3M " + f2(r["r3m"]) +
                     "% | 6M " + f2(r["r6m"]) + "% | 1Y " + f2(r["r1y"]) + "%" +
                     (" | 3Y CAGR " + format(r["cagr3"], "+.1f") + "%" if r["cagr3"] is not None else "") +
                     (" | 5Y CAGR " + format(r["cagr5"], "+.1f") + "%" if r["cagr5"] is not None else ""))
        lines.append("- Nearby levels: support ~" + money(r["sup3"]) + " (3-mo low) | resistance ~" + money(r["res3"]) + " (3-mo high)")
        if r["notes"]:
            lines.append("- Notes: " + "; ".join(r["notes"]))
        if r.get("fundamentals"):
            lines.append("- Fundamentals: " + " | ".join(k + " " + v for k, v in r["fundamentals"]))
        chart = r["symbol"] + ".png"
        lines.append("- Chart: charts/" + chart)
    lines += ["", "---", "", "*Generated by analyze.py. Data: Yahoo Finance (NSE). Rule-based signals only - verify before trading; not investment advice.*"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Short/long-term trend analysis for NSE stocks.")
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. RELIANCE TCS or indexes NIFTY BANKNIFTY")
    ap.add_argument("--watchlist", help="file with one symbol per line")
    ap.add_argument("--period", default="5y", help="history window for data download (default 5y)")
    ap.add_argument("--refresh", action="store_true", help="ignore intraday cache and re-download")
    ap.add_argument("--fundamentals", action="store_true", help="also fetch P/E, ROE, margins, growth, dividend yield")
    ap.add_argument("--no-charts", action="store_true", help="skip chart generation")
    args = ap.parse_args()

    symbols = list(args.symbols)
    if args.watchlist:
        symbols += engine.read_watchlist(args.watchlist)
    seen, uniq = set(), []
    for s in symbols:
        if s.upper() not in seen:
            seen.add(s.upper())
            uniq.append(s)
    symbols = uniq
    if not symbols:
        ap.error("give at least one symbol, or --watchlist <file>")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(CHARTS_DIR, exist_ok=True)

    results = []
    for sym in symbols:
        try:
            df, ym, cached = engine.fetch(sym, args.period, args.refresh)
            if df is None:
                print("[skip] " + sym + " : no data returned")
                continue
            r, d = engine.metrics(sym, df, ym)
            if args.fundamentals and not ym.startswith("^"):
                r["fundamentals"] = fundamentals(ym, r["close"])
            if not args.no_charts:
                make_chart(ym, d, os.path.join(CHARTS_DIR, r["symbol"] + ".png"))
            results.append(r)
            tag = "cache" if cached else "fresh"
            print("[" + tag + "] " + r["symbol"] + " " + money(r["close"]) +
                  " | ST " + r["st_label"] + " | LT " + r["lt_label"])
        except Exception as e:
            print("[error] " + sym + " : " + str(e))

    if not results:
        sys.exit("No symbols processed.")

    generated = dt.date.today().isoformat()
    report = build_report(results, generated)
    path = os.path.join(REPORTS_DIR, "trend_report_" + generated + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report)

    table = pd.DataFrame([{
        "Symbol": r["symbol"],
        "CMP": round(r["close"], 1),
        "1M%": None if r["r1m"] is None else round(r["r1m"], 1),
        "RSI": None if r["rsi"] is None else round(r["rsi"], 1),
        "vs200DMA%": None if not r["sma200"] else round((r["close"] / r["sma200"] - 1) * 100, 1),
        "Short-term": r["st_label"],
        "Long-term": r["lt_label"],
    } for r in results])
    print()
    print(table.to_string(index=False))
    print()
    print("Report: " + path)


if __name__ == "__main__":
    main()
