"""IPO desk - listing-day play for upcoming and freshly listed IPOs.

Data: investorgain.com JSON reports (the same feed their live pages call):
  331 - live GMP board: upcoming / open / closed / listed, with GMP, subscription,
        issue price, size, lot, P/E, dates, their heat rating
  377 - GMP performance tracker: ~250 recent listings with GMP, issue price,
        actual listing price, listing-day close and LTP -> used to calibrate
  333 - live subscription: QIB / NII / retail breakup for issues being bid

Empirical base rate from the last ~250 listings (pulled 2026-10-01):
  GMP >= 20%: 98% of listings opened positive, median listing +43.6%
  GMP 10-20%: 91% positive, median +16.7%
  GMP  5-10%: 71% positive, median  +6.1%  (no edge for a buy-at-listing entry)
  GMP   0-5%: ~coin flip
  GMP  <= 0 : 32% positive, mean -5.1% -> avoid
Buy at listing -> hold to listing-day close (median drift):
  GMP >= 20%: +7.0% (67% close above listing, p25 -6.6%)
  GMP 10-20%: +5.9% (78% close above listing)
  else      : ~0 -> skip

So the desk rule: only GMP >= ~10% (ideally >= 20% with strong subscription),
enter near the GMP-implied price on listing day, small target, hard stop.

Read-only research: nothing here places orders. GMP is an unofficial grey-market
indicator - it can change or break between close and listing.
"""
import datetime as dt
import html as _html
import json
import os
import re
import statistics as st

import requests

from swing import engine

CACHE_DIR = os.path.join(engine.DATA_DIR, "ipo_cache")
CACHE_TTL_MIN = 20
REPORT_BASE = "https://webnodejs.investorgain.com/cloud/v2/report/data-read/"

STATUS = {"U": "Upcoming", "O": "Open", "C": "Closed", "LP": "Listed", "LN": "Listed"}


def _headers(rep):
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.investorgain.com",
        "Referer": "https://www.investorgain.com/report/ipo-gmp-live/" + str(rep) + "/",
        "sec-ch-ua": '"Chromium";v="150", "Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


def _fy_segments():
    today = dt.date.today()
    y = today.year if today.month >= 4 else today.year - 1
    return str(y), str(y) + "-" + str(y + 1)[-2:]


def _fetch_report(rep, refresh=False):
    """Raw rows of one investorgain report (cached on disk for a short TTL)."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:
        pass
    path = os.path.join(CACHE_DIR, "report_" + str(rep) + ".json")
    if os.path.exists(path) and not refresh:
        age_min = (dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 60
        if age_min < CACHE_TTL_MIN:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    yr, fy = _fy_segments()
    url = REPORT_BASE + str(rep) + "/1/10/" + yr + "/" + fy + "/0/all?search=&v=22-18"
    r = requests.get(url, headers=_headers(rep), timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("msg") != 1:
        raise RuntimeError("investorgain report " + str(rep) + ": " + str(data.get("error") or "no data"))
    rows = data.get("reportTableData") or []
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    except Exception:
        pass
    return rows


def _txt(x):
    if x is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(x))
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _num(x):
    if x is None:
        return None
    m = re.search(r"(-?\d[\d,]*\.?\d*)", _html.unescape(str(x)))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _iso(x):
    s = _txt(x)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _flames(x):
    return str(x or "").count("🔥")


def _current_gmp(cell):
    """First <b>NN</b> in the GMP cell is the live GMP; small text holds min/max."""
    s = str(cell or "")
    m = re.search(r"<b>\s*(-?[\d.]+)\s*</b>", s)
    return float(m.group(1)) if m else None


def _milestone_gmp(cell):
    """'GMP: NN' note attached to the Open / Close / Listing date cells."""
    m = re.search(r"GMP:\s*(-?[\d.]+)", str(cell or ""))
    return float(m.group(1)) if m else None


def _clean_issue_name(s):
    s = _txt(s)
    s = re.split(r"\s+(?:BSE|NSE)\b|\s+GMP\b", s)[0]
    return s.strip(" -|")


def _days_to(d):
    if d is None:
        return None
    return (d - dt.date.today()).days


def board(refresh=False):
    """Live GMP board split by status. One dict per IPO."""
    rows = _fetch_report(331, refresh)
    out = []
    for x in rows:
        price = _num(x.get("Price (₹)"))
        gmp_pct = _num(x.get("~gmp_percent_calc"))
        gmp_rs = _current_gmp(x.get("GMP"))
        if gmp_rs is None and price is not None and gmp_pct is not None:
            gmp_rs = round(price * gmp_pct / 100, 1)
        cat = _txt(x.get("~IPO_Category")) or "IPO"
        status = str(x.get("~ipo_status1") or "?")
        rec = {
            "id": str(x.get("~id") or ""),
            "name": _txt(x.get("~ipo_name")),
            "cat": cat,
            "status": status,
            "status_txt": STATUS.get(status, status),
            "gmp_rs": gmp_rs,
            "gmp_peak": _num(x.get("~max_gmp1")),
            "gmp_pct": gmp_pct,
            "gmp_open": _milestone_gmp(x.get("Open")),
            "gmp_close": _milestone_gmp(x.get("Close")),
            "gmp_listing": _milestone_gmp(x.get("Listing")),
            "sub": _num(x.get("Sub")),
            "price": price,
            "size_cr": _num(x.get("IPO Size")),
            "lot": _num(x.get("Lot")),
            "pe": _num(x.get("~P/E")),
            "rating": _flames(x.get("Rating")),
            "open_dt": _iso(x.get("~Srt_Open")),
            "close_dt": _iso(x.get("~Srt_Close")),
            "boa_dt": _iso(x.get("~Srt_BoA_Dt")),
            "listing_dt": _iso(x.get("~Str_Listing")),
            "updated": _txt(x.get("Updated-On")),
            "url": "https://www.investorgain.com" + str(x.get("~urlrewrite_folder_name") or ""),
        }
        if price and gmp_pct is not None:
            rec["exp_listing"] = round(price * (1 + gmp_pct / 100), 2)
        else:
            rec["exp_listing"] = None
        out.append(rec)
    return out


def performance(refresh=False):
    """GMP performance tracker: recent listings with GMP vs actual outcomes."""
    rows = _fetch_report(377, refresh)
    out = []
    for x in rows:
        price = _num(x.get("Price"))
        gmp = _num(x.get("GMP"))
        listing_gain = _num(x.get("~str_listing_gain_in_per"))
        close_gain = _num(x.get("~str_closing_gain_in_per"))
        ltp_gain = _num(x.get("~str_ltp_per_calc"))
        sym = _txt(x.get("Symbol")).split(",")[0].strip()
        rec = {
            "name": _txt(x.get("IPO")),
            "symbol": sym,
            "cat": _txt(x.get("~IPO_Category")) or "IPO",
            "listing_dt": _txt(x.get("Listing Dt")),
            "size_cr": _num(x.get("Size")),
            "sub": _num(x.get("Sub")),
            "gmp_rs": gmp,
            "price": price,
            "gmp_pct": round(gmp / price * 100, 1) if (price and gmp is not None) else None,
            "listing_gain": listing_gain,
            "close_gain": close_gain,
            "ltp_gain": ltp_gain,
            "rating": _num(x.get("~srt_gmp_rating")),
            "id": str(x.get("~id") or ""),
            "url": "https://www.investorgain.com" + str(x.get("~URLRewrite_Folder_Name") or ""),
        }
        if rec["listing_gain"] is not None and rec["close_gain"] is not None:
            rec["drift"] = round(rec["close_gain"] - rec["listing_gain"], 1)
        else:
            rec["drift"] = None
        out.append(rec)
    return out


def subscription(refresh=False):
    """Live subscription breakup for issues being bid (QIB/NII/retail)."""
    rows = _fetch_report(333, refresh)
    out = []
    for x in rows:
        out.append({
            "id": str(x.get("~id") or ""),
            "name": _clean_issue_name(x.get("Name")),
            "cat": _txt(x.get("~IPO_Category")) or "IPO",
            "total": _num(x.get("Total")),
            "qib": _num(x.get("QIB")),
            "nii": _num(x.get("NII")),
            "retail": _num(x.get("RII")),
            "size_cr": _num(x.get("IPO Size")),
            "price": _num(x.get("IPO Price")),
            "pe": _num(x.get("P/E")),
            "close_dt": _txt(x.get("Closing Date")),
            "anchor": "✅" in str(x.get("Anchor") or ""),
        })
    return out


def bucket_stats(perf, min_gmp=10.0):
    """Empirical table: GMP% bucket -> listing outcome + at-listing entry outcome.

    Returns (buckets, headline) where headline is the >= min_gmp aggregate used
    for the desk verdict.
    """
    def bucket(g):
        if g >= 20:
            return "GMP >= 20%"
        if g >= 10:
            return "GMP 10-20%"
        if g >= 5:
            return "GMP 5-10%"
        if g >= 1:
            return "GMP 1-5%"
        return "GMP <= 1%"

    order = ["GMP >= 20%", "GMP 10-20%", "GMP 5-10%", "GMP 1-5%", "GMP <= 1%"]
    groups = {k: [] for k in order}
    for r in perf:
        if r.get("gmp_pct") is None or r.get("listing_gain") is None:
            continue
        groups[bucket(r["gmp_pct"])].append(r)

    buckets = []
    for k in order:
        v = groups[k]
        if not v:
            continue
        gains = [x["listing_gain"] for x in v]
        drifts = [x["drift"] for x in v if x["drift"] is not None]
        buckets.append({
            "bucket": k,
            "n": len(v),
            "listing_median": round(st.median(gains), 1),
            "listing_pos_pct": round(sum(1 for g in gains if g > 0) / len(gains) * 100, 0),
            "drift_median": round(st.median(drifts), 1) if drifts else None,
            "drift_pos_pct": round(sum(1 for d in drifts if d > 0) / len(drifts) * 100, 0) if drifts else None,
            "drift_worst": round(min(drifts), 1) if drifts else None,
        })

    hi = [r for r in perf if r.get("gmp_pct") is not None and r["gmp_pct"] >= min_gmp
          and r.get("listing_gain") is not None]
    if hi:
        gains = [x["listing_gain"] for x in hi]
        drifts = [x["drift"] for x in hi if x["drift"] is not None]
        headline = {
            "n": len(hi),
            "listing_median": round(st.median(gains), 1),
            "listing_pos_pct": round(sum(1 for g in gains if g > 0) / len(gains) * 100, 0),
            "drift_median": round(st.median(drifts), 1) if drifts else None,
            "drift_pos_pct": round(sum(1 for d in drifts if d > 0) / len(drifts) * 100, 0) if drifts else None,
            "drift_p25": round(st.quantiles(drifts, n=4)[0], 1) if len(drifts) >= 4 else None,
            "drift_worst": round(min(drifts), 1) if drifts else None,
        }
    else:
        headline = None
    return buckets, headline


def match_subs(name, subs):
    """Find the subscription row for a board IPO by loose name match."""
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

    n = norm(name)
    if not n:
        return None
    for s in subs or []:
        m = norm(s.get("name"))
        if m and (m in n or n in m):
            return s
    return None


def score(ipo, subs=None):
    """Listing-pop score 0-10 from GMP, subscription, size, QIB, their heat rating."""
    pts, notes = 0.0, []
    g = ipo.get("gmp_pct")
    if g is not None:
        if g >= 20:
            pts += 4.5
            notes.append("GMP >= 20% bucket: 98% listed positive historically, median +44%")
        elif g >= 10:
            pts += 3.0
            notes.append("GMP 10-20% bucket: 91% listed positive, median +17%")
        elif g >= 5:
            pts += 1.0
            notes.append("GMP 5-10% bucket: 71% positive, median +6% - thin edge")
        elif g >= 1:
            pts += 0.25
            notes.append("GMP 1-5%: coin flip historically")
        else:
            notes.append("GMP <= 1%: no listing edge (32% positive, mean -5%)")

    s = ipo.get("sub")
    if s is not None:
        if s >= 25:
            pts += 2.0
            notes.append("Subscription " + format(s, ".1f") + "x - very strong demand")
        elif s >= 10:
            pts += 1.5
            notes.append("Subscription " + format(s, ".1f") + "x - strong demand")
        elif s >= 3:
            pts += 0.75
        elif s >= 1:
            pts += 0.25

    size = ipo.get("size_cr")
    if size is not None:
        if size <= 50:
            pts += 1.0
            notes.append("Small issue (Rs " + format(size, ",.0f") + " Cr) - moves more easily")
        elif size <= 150:
            pts += 0.75
        elif size <= 500:
            pts += 0.25

    if subs:
        q = subs.get("qib")
        if q is not None:
            if q >= 10:
                pts += 1.0
                notes.append("QIB " + format(q, ".1f") + "x - institutions are in")
            elif q >= 2:
                pts += 0.5
            elif q >= 0.8:
                pts += 0.25

    r = ipo.get("rating")
    if r is not None:
        if r >= 3:
            pts += 1.0
        elif r >= 2:
            pts += 0.5
        elif r >= 1:
            pts += 0.25

    pk, cur = ipo.get("gmp_peak"), ipo.get("gmp_rs")
    if pk and cur is not None and pk > 0 and cur < 0.5 * pk:
        notes.append("GMP collapsed from peak Rs " + format(pk, ",.0f") + " to Rs " + format(cur, ",.1f") +
                     " - grey market has cooled; treat the edge as much weaker")

    score_val = round(min(pts, 10.0), 1)
    if score_val >= 7:
        label = "A - strong pop odds"
    elif score_val >= 5:
        label = "B - moderate"
    elif score_val >= 3:
        label = "C - thin"
    else:
        label = "Skip"
    if ipo.get("cat") == "SME" and score_val >= 5:
        notes.append("SME: bigger pops but 5% circuits, thin books and lot-size minimums - "
                     "small size or skip if one lot > your budget")
    return score_val, label, notes


def trail_stop(entry, peak, giveback_pct=30.0, initial_stop=None):
    """Ratcheting trailing stop: give back at most giveback_pct of the max profit.

    peak 120 on entry 100 at 30% -> stop 114 (locks 14 of the 20).
    peak 130 -> stop 121. Until the peak is above entry the initial stop holds.
    """
    if peak is None or peak <= entry:
        return initial_stop if initial_stop is not None else entry * 0.95
    stop = peak - (giveback_pct / 100.0) * (peak - entry)
    if initial_stop is not None:
        stop = max(stop, initial_stop)
    return stop


def trail_ladder(entry, qty, initial_stop, giveback_pct=30.0, peaks_pct=(3, 5, 8, 10, 15, 20, 30)):
    """Worked examples of the trail: peak -> stop -> locked profit."""
    out = []
    for p in peaks_pct:
        peak = entry * (1 + p / 100.0)
        stop = trail_stop(entry, peak, giveback_pct, initial_stop)
        locked = (stop - entry) * qty
        out.append({
            "peak": round(peak, 2),
            "peak_pct": p,
            "profit_at_peak": round((peak - entry) * qty, 0),
            "stop": round(stop, 2),
            "locked_rs": round(locked, 0),
            "locked_pct": round((stop / entry - 1) * 100, 1),
        })
    return out


def listing_plan(ipo, capital, risk_pct, giveback_pct=30.0):
    """Conditional listing-day ticket. Entry near the GMP-implied price, small target,
    hard stop, size from the risk settings. Lot-rounded for SME."""
    price = ipo.get("price")
    gmp_pct = ipo.get("gmp_pct")
    if not price or gmp_pct is None:
        return None
    exp = price * (1 + gmp_pct / 100)
    sme = ipo.get("cat") == "SME"
    stop_pct = 7.0 if sme else 5.0
    tgt_pct = 7.0 if sme else 5.0
    stretch_pct = 12.0 if sme else 8.0

    entry = round(exp, 2)
    stop = round(entry * (1 - stop_pct / 100), 2)
    target = round(entry * (1 + tgt_pct / 100), 2)
    stretch = round(entry * (1 + stretch_pct / 100), 2)

    risk_amt = capital * risk_pct / 100.0
    stop_dist = entry - stop
    qty = int(risk_amt / stop_dist) if stop_dist > 0 else 0
    cap_qty = int(capital * 0.25 / entry) if entry > 0 else 0
    qty = min(qty, cap_qty)

    flags = []
    lot = ipo.get("lot") or 1
    if sme:
        lot_qty = int(qty // lot) * int(lot)
        if lot_qty == 0:
            one_lot_val = round(lot * entry, 0)
            flags.append("One SME lot costs about Rs " + format(one_lot_val, ",.0f") +
                         " - above your risk budget; size up capital or skip")
            qty = 0
        else:
            qty = lot_qty
    if qty == 0 and not flags:
        flags.append("Position size is 0 at current capital/risk settings")

    value = round(qty * entry, 0)
    risk_rs = round((entry - stop) * qty, 0)
    reward_rs = round((target - entry) * qty, 0)
    plan = {
        "exp_listing": round(exp, 2),
        "entry": entry,
        "zone_lo": round(exp * 0.95, 2),
        "zone_hi": round(exp * 1.05, 2),
        "no_chase_above": round(exp * 1.25, 2),
        "skip_below": round(exp * 0.85, 2),
        "stop": stop,
        "stop_pct": stop_pct,
        "target": target,
        "target_pct": tgt_pct,
        "stretch": stretch,
        "stretch_pct": stretch_pct,
        "qty": qty,
        "lot": lot,
        "value": value,
        "risk_rs": risk_rs,
        "reward_rs": reward_rs,
        "risk_pct_rs": capital and round(risk_rs / capital * 100, 2),
        "reward_pct_rs": capital and round(reward_rs / capital * 100, 2),
        "sme": sme,
        "giveback_pct": giveback_pct,
        "flags": flags,
    }
    plan["trail"] = trail_ladder(entry, qty, stop, giveback_pct) if qty > 0 else []
    return plan


def board_split(rows, today=None):
    """Group board rows: open now, listing soon (closed), upcoming, recent listings."""
    today = today or dt.date.today()
    out = {"open": [], "soon": [], "upcoming": [], "listed": []}
    for r in rows:
        d = _days_to(r.get("listing_dt"))
        if r["status"] == "O":
            out["open"].append(r)
        elif r["status"] == "C":
            out["soon"].append(r)
        elif r["status"] == "U":
            out["upcoming"].append(r)
        elif r["status"] in ("LP", "LN"):
            out["listed"].append(r)
        else:
            out["upcoming"].append(r)
    out["open"].sort(key=lambda r: (r.get("close_dt") or today))
    out["soon"].sort(key=lambda r: (r.get("listing_dt") or today))
    out["upcoming"].sort(key=lambda r: (r.get("open_dt") or today))
    out["listed"].sort(key=lambda r: (r.get("listing_dt") or today), reverse=True)
    return out
