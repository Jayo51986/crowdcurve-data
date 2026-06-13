#!/usr/bin/env python3
"""
CrowdCurve data engine.

Polls free, keyless public APIs:
  - Binance (spot prices), CoinGecko free endpoint as fallback
  - Polymarket Gamma API (threshold markets + probabilities + volume)
  - Kalshi public market API (threshold markets + probabilities + volume)

Normalizes everything into per-coin, per-horizon consensus data, computes the
summary numbers (peak, band, width) with the same bucket math as the plugin,
and writes:

  data/config.json          full payload the plugin's JS layer consumes
  data/{COIN}/{HORIZON}     summary JSON the plugin's PHP layer consumes
  data/history/{date}.json  daily snapshot: the irreplaceable historical record

Run by GitHub Actions every 20 minutes (see .github/workflows/poll.yml).
Standard library only; no pip installs needed.
"""

import json
import math
import os
import re
import sys
import time
import datetime as dt
import urllib.request
import urllib.error

OUT_DIR = "data"
UA = {"User-Agent": "CrowdCurveBot/1.0 (+https://cryptocurrencypricesnow.com/forecast)"}

COINS = {
    "BTC":  {"name": "Bitcoin",     "aliases": ["bitcoin", "btc"],       "binance": "BTCUSDT",  "gecko": "bitcoin"},
    "ETH":  {"name": "Ethereum",    "aliases": ["ethereum", "eth"],      "binance": "ETHUSDT",  "gecko": "ethereum"},
    "SOL":  {"name": "Solana",      "aliases": ["solana", "sol"],        "binance": "SOLUSDT",  "gecko": "solana"},
    "XRP":  {"name": "XRP",         "aliases": ["xrp", "ripple"],        "binance": "XRPUSDT",  "gecko": "ripple"},
    "DOGE": {"name": "Dogecoin",    "aliases": ["dogecoin", "doge"],     "binance": "DOGEUSDT", "gecko": "dogecoin"},
    "LTC":  {"name": "Litecoin",    "aliases": ["litecoin", "ltc"],      "binance": "LTCUSDT",  "gecko": "litecoin"},
    "BNB":  {"name": "BNB",         "aliases": ["bnb", "binance coin"],  "binance": "BNBUSDT",  "gecko": "binancecoin"},
    "ADA":  {"name": "Cardano",     "aliases": ["cardano", "ada"],       "binance": "ADAUSDT",  "gecko": "cardano"},
    "HBAR": {"name": "Hedera",      "aliases": ["hedera", "hbar"],       "binance": "HBARUSDT", "gecko": "hedera-hashgraph"},
    "ZEC":  {"name": "Zcash",       "aliases": ["zcash", "zec"],         "binance": "ZECUSDT",  "gecko": "zcash"},
    "HYPE": {"name": "Hyperliquid", "aliases": ["hyperliquid", "hype"],  "binance": "HYPEUSDT", "gecko": "hyperliquid"},
}

KALSHI_TICKER_HINTS = {
    "KXBTC": "BTC", "KXETH": "ETH", "KXSOL": "SOL",
    "KXXRP": "XRP", "KXDOGE": "DOGE", "KXLTC": "LTC",
}

PRICE_RE = re.compile(r"\$\s?(\d[\d,]*\.?\d*)\s*([kKmM]?)")
ABOVE_RE = re.compile(r"\b(above|hit|hits|reach|reaches|exceed|exceeds|close above|at or above|or above|touch)\b", re.I)
BELOW_RE = re.compile(r"\b(below|under|dip|dips|drop to|fall to|less than)\b", re.I)

LOG = []


def log(msg):
    line = f"[{dt.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def get_json(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
        log(f"fetch failed: {url} -> {e}")
        return None


def clamp(v, lo=0.01, hi=0.99):
    return min(hi, max(lo, v))


# ── spot prices ──────────────────────────────────────────────────────────────

def fetch_spots():
    spots = {}
    for sym, c in COINS.items():
        d = get_json(f"https://api.binance.com/api/v3/ticker/price?symbol={c['binance']}")
        if d and "price" in d:
            try:
                spots[sym] = float(d["price"])
            except (TypeError, ValueError):
                pass
        time.sleep(0.15)
    missing = [s for s in COINS if s not in spots]
    if missing:
        ids = ",".join(COINS[s]["gecko"] for s in missing)
        d = get_json(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd")
        if d:
            for s in missing:
                g = COINS[s]["gecko"]
                if g in d and "usd" in d[g]:
                    spots[s] = float(d[g]["usd"])
    log(f"spot prices: {len(spots)}/{len(COINS)} coins -> " + ", ".join(f"{k} {v:,.2f}" for k, v in spots.items()))
    return spots


# ── market parsing helpers ───────────────────────────────────────────────────

def coin_in(text):
    t = " " + text.lower() + " "
    for sym, c in COINS.items():
        for a in c["aliases"]:
            if re.search(rf"\b{re.escape(a)}\b", t):
                return sym
    return None


def parse_price(text):
    m = PRICE_RE.search(text)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    suffix = m.group(2).lower()
    if suffix == "k":
        v *= 1_000
    elif suffix == "m":
        v *= 1_000_000
    return v


def parse_direction(text):
    if BELOW_RE.search(text):
        return "below"
    if ABOVE_RE.search(text):
        return "above"
    return None


def parse_date(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ── Polymarket ───────────────────────────────────────────────────────────────

def fetch_polymarket():
    out = []
    offset = 0
    while offset < 3000:
        d = get_json(f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}")
        if d is None:
            break
        if isinstance(d, dict):  # tolerate wrapped responses
            d = d.get("markets") or d.get("data") or []
        if not isinstance(d, list) or not d:
            break
        for mkt in d:
            try:
                q = str(mkt.get("question") or mkt.get("title") or "")
                sym = coin_in(q)
                if not sym:
                    continue
                strike = parse_price(q)
                direction = parse_direction(q)
                if strike is None or direction is None:
                    continue
                prob = None
                op = mkt.get("outcomePrices")
                if op:
                    arr = json.loads(op) if isinstance(op, str) else op
                    if isinstance(arr, list) and arr:
                        prob = float(arr[0])
                if prob is None:
                    for f in ("lastTradePrice", "bestBid", "bestAsk"):
                        if mkt.get(f) is not None:
                            prob = float(mkt[f])
                            break
                if prob is None:
                    continue
                if direction == "below":
                    prob = 1.0 - prob
                end = parse_date(mkt.get("endDate") or mkt.get("end_date_iso") or mkt.get("endDateIso"))
                if end is None:
                    continue
                vol = float(mkt.get("volumeNum") or mkt.get("volume") or 0)
                out.append({"venue": "pm", "coin": sym, "strike": strike,
                            "prob": clamp(prob), "vol": vol, "end": end, "q": q})
            except (TypeError, ValueError, KeyError):
                continue
        if len(d) < 100:
            break
        offset += 100
        time.sleep(1.2)  # stay well under unauthenticated rate limits
    log(f"polymarket: {len(out)} crypto threshold markets parsed")
    return out


# ── Kalshi ───────────────────────────────────────────────────────────────────

def kalshi_coin(mkt):
    ticker = str(mkt.get("ticker") or "")
    for prefix, sym in KALSHI_TICKER_HINTS.items():
        if ticker.startswith(prefix):
            return sym
    return coin_in(str(mkt.get("title") or "") + " " + str(mkt.get("subtitle") or ""))


def fetch_kalshi():
    out = []
    cursor = ""
    base = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=200"
    for _ in range(25):
        url = base + (f"&cursor={cursor}" if cursor else "")
        d = get_json(url)
        if not d or "markets" not in d:
            break
        for mkt in d["markets"]:
            try:
                sym = kalshi_coin(mkt)
                if not sym:
                    continue
                text = str(mkt.get("title") or "") + " " + str(mkt.get("subtitle") or "")
                strike = None
                if mkt.get("floor_strike") is not None:
                    strike = float(mkt["floor_strike"])
                    direction = "above"
                else:
                    strike = parse_price(text)
                    direction = parse_direction(text)
                if strike is None or direction is None:
                    continue
                prob = None
                bid, ask = mkt.get("yes_bid"), mkt.get("yes_ask")
                if bid is not None and ask is not None and (bid or ask):
                    prob = ((float(bid) + float(ask)) / 2.0) / 100.0
                elif mkt.get("last_price") is not None:
                    prob = float(mkt["last_price"]) / 100.0
                if prob is None or prob <= 0:
                    continue
                if direction == "below":
                    prob = 1.0 - prob
                end = parse_date(mkt.get("close_time") or mkt.get("expiration_time"))
                if end is None:
                    continue
                vol = float(mkt.get("volume") or 0)
                out.append({"venue": "k", "coin": sym, "strike": strike,
                            "prob": clamp(prob), "vol": vol, "end": end, "q": text.strip()})
            except (TypeError, ValueError, KeyError):
                continue
        cursor = d.get("cursor") or ""
        if not cursor:
            break
        time.sleep(0.7)
    log(f"kalshi: {len(out)} crypto threshold markets parsed")
    return out


# ── horizons ─────────────────────────────────────────────────────────────────

def month_end(d):
    nxt = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return nxt - dt.timedelta(days=1)


def quarter_end(d):
    qm = ((d.month - 1) // 3 + 1) * 3
    return month_end(dt.date(d.year, qm, 1))


def horizon_targets(today):
    """key -> (target date, tolerance in days)"""
    me = month_end(today)
    if (me - today).days < 4:  # month nearly over: roll to next month
        me = month_end(me + dt.timedelta(days=4))
    qe = quarter_end(today)
    if (qe - today).days < 12:
        qe = quarter_end(qe + dt.timedelta(days=12))
    return {
        "W":  (today + dt.timedelta(days=7), 4),
        "M":  (me, 7),
        "Q":  (qe, 16),
        "Y":  (dt.date(today.year, 12, 31), 24),
        "Y1": (dt.date(today.year + 1, 12, 31), 45),
        "Y2": (dt.date(today.year + 2, 12, 31), 60),
    }


def assign_horizon(end, targets):
    best, best_d = None, 10 ** 9
    for key, (target, tol) in targets.items():
        diff = abs((end - target).days)
        if diff <= tol and diff < best_d:
            best, best_d = key, diff
    return best


# ── consensus math (same bucket method as the plugin) ────────────────────────

def merge_strikes(points):
    """Group venue points at near-identical strikes into rows of
    {strike, pm, k, pmVol, kVol}, monotonically cleaned."""
    points = sorted(points, key=lambda p: p["strike"])
    rows = []
    for p in points:
        if rows and abs(p["strike"] - rows[-1]["strike"]) / max(rows[-1]["strike"], 1e-9) < 0.005:
            row = rows[-1]
        else:
            row = {"strike": p["strike"], "pm": None, "k": None, "pmVol": 0, "kVol": 0}
            rows.append(row)
        if p["venue"] == "pm":
            row["pm"] = p["prob"] if row["pm"] is None else (row["pm"] + p["prob"]) / 2
            row["pmVol"] += p["vol"]
        else:
            row["k"] = p["prob"] if row["k"] is None else (row["k"] + p["prob"]) / 2
            row["kVol"] += p["vol"]
    for row in rows:  # single-venue rows mirror the present venue
        if row["pm"] is None:
            row["pm"] = row["k"]
        if row["k"] is None:
            row["k"] = row["pm"]
    # enforce non-increasing survival as strikes rise
    prev = 1.0
    for row in rows:
        avg = (row["pm"] + row["k"]) / 2
        if avg > prev:
            scale = prev / avg if avg else 1
            row["pm"] = clamp(row["pm"] * scale)
            row["k"] = clamp(row["k"] * scale)
        prev = (row["pm"] + row["k"]) / 2
    return rows


def summarize(spot, rows):
    strikes = [r["strike"] for r in rows]
    tot = sum(r["pmVol"] + r["kVol"] for r in rows) or 1.0

    def cons(r):
        v = r["pmVol"] + r["kVol"]
        if v > 0:
            return (r["pm"] * r["pmVol"] + r["k"] * r["kVol"]) / v
        return (r["pm"] + r["k"]) / 2

    surv = [1.0] + [clamp(cons(r), 0.0, 1.0) for r in rows] + [0.0]
    for i in range(1, len(surv)):
        surv[i] = min(surv[i], surv[i - 1])
    floor = min(strikes[0] * 0.85, spot * 0.6)
    cap = max(strikes[-1] * 1.12, spot * 1.7)
    edges = [floor] + strikes + [cap]
    expected = 0.0
    for i in range(len(edges) - 1):
        expected += ((edges[i] + edges[i + 1]) / 2) * max(0.0, surv[i] - surv[i + 1])

    def quantile(q):
        for i in range(len(surv) - 1):
            if surv[i] >= q >= surv[i + 1]:
                t = (surv[i] - q) / ((surv[i] - surv[i + 1]) or 1)
                return edges[i] + t * (edges[i + 1] - edges[i])
        return edges[-1]

    median, p10, p90 = quantile(0.5), quantile(0.9), quantile(0.1)
    width = (p90 - p10) / median * 100 if median else 0
    return {"peak": expected, "median": median, "band_lo": p10, "band_hi": p90,
            "width": round(width, 1), "volume": round(tot)}


# ── output writers ───────────────────────────────────────────────────────────

def fmt_money(v):
    if v >= 1000:
        return "$" + f"{round(v):,}"
    if v >= 10:
        return "$" + str(round(v))
    return "$" + f"{v:.2f}"


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def coin_summary_text(sym, name, spot, summary, resolve_label):
    delta = (summary["peak"] - spot) / spot * 100 if spot else 0
    direction = "above" if delta >= 0 else "below"
    intro = (
        f"{name} is trading at {fmt_money(spot)} right now. Traders putting real money on prediction markets "
        f"currently expect {name} near {fmt_money(summary['peak'])} by {resolve_label}. That is about "
        f"{abs(delta):.1f}% {direction} today's price. Most of them see it landing somewhere between "
        f"{fmt_money(summary['band_lo'])} and {fmt_money(summary['band_hi'])}. Live numbers from Polymarket "
        f"and Kalshi, refreshed every 20 minutes."
    )
    return {
        "h1": f"{name} ({sym}) Price Forecast: What the Market Expects by {resolve_label}",
        "intro": intro,
        "noscript": intro,
        "peak": round(summary["peak"], 4),
        "band_lo": round(summary["band_lo"], 4),
        "band_hi": round(summary["band_hi"], 4),
        "width": summary["width"],
    }


def no_market_text(sym, name, spot, horizon_label):
    intro = (
        f"{name} is trading at {fmt_money(spot)} right now. There are no live prediction markets for {name} "
        f"at the {horizon_label} horizon at the moment, and we will not invent a forecast where no real money "
        f"is at stake. We check Polymarket and Kalshi every 20 minutes, and this page upgrades itself the "
        f"moment real markets appear."
    )
    return {"h1": f"{name} ({sym}) Price Now and Forecast Status",
            "intro": intro, "noscript": intro, "peak": 0}


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    today = dt.datetime.utcnow().date()
    targets = horizon_targets(today)
    spots = fetch_spots()
    if not spots:
        log("FATAL: no spot prices available; aborting without writing")
        sys.exit(1)

    raw = fetch_polymarket() + fetch_kalshi()
    log(f"total raw threshold markets: {len(raw)}")

    # bucket: coin -> horizon -> [points]
    grouped = {}
    for p in raw:
        h = assign_horizon(p["end"], targets)
        if not h:
            continue
        grouped.setdefault(p["coin"], {}).setdefault(h, []).append(p)

    config = {"generated_at": dt.datetime.utcnow().isoformat() + "Z", "coins": {}}
    hist_dir = os.path.join(OUT_DIR, "history")
    existing_hist = sorted(os.listdir(hist_dir)) if os.path.isdir(hist_dir) else []
    config["recording_since"] = (existing_hist[0].replace(".json", "") if existing_hist else today.isoformat())

    horizon_keys = ["W", "M", "Q", "Y", "Y1", "Y2"]
    for sym, c in COINS.items():
        name = c["name"]
        spot = spots.get(sym)
        coin_out = {"name": name, "spot": spot, "horizons": {}}
        valid = []
        for hk in horizon_keys:
            target, _tol = targets[hk]
            label = target.strftime("%b %-d, %Y") if os.name != "nt" else target.strftime("%b %d, %Y")
            pts = grouped.get(sym, {}).get(hk, [])
            rows = merge_strikes(pts) if pts else []
            if spot and len(rows) >= 3:
                # use the modal end date of the actual markets as the label
                ends = sorted(p["end"] for p in pts)
                label = ends[len(ends) // 2].strftime("%b %d, %Y").replace(" 0", " ")
                summary = summarize(spot, rows)
                coin_out["horizons"][hk] = {
                    "resolve": label,
                    "markets": [{"strike": r["strike"], "pm": round(r["pm"], 4), "k": round(r["k"], 4),
                                 "pmVol": round(r["pmVol"]), "kVol": round(r["kVol"])} for r in rows],
                    "summary": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()},
                }
                valid.append(hk)
                write_json(os.path.join(OUT_DIR, sym, hk),
                           coin_summary_text(sym, name, spot, summary, label))
            else:
                if spot:
                    write_json(os.path.join(OUT_DIR, sym, hk),
                               no_market_text(sym, name, spot, hk))
        coin_out["tier"] = "full" if ("M" in valid and len(valid) >= 3) else ("partial" if valid else "spot")
        config["coins"][sym] = coin_out
        log(f"{sym}: tier={coin_out['tier']} horizons={valid} spot={spot}")

    write_json(os.path.join(OUT_DIR, "config.json"), config)
    # daily history snapshot: latest run of each UTC day wins
    write_json(os.path.join(OUT_DIR, "history", today.isoformat() + ".json"), config)
    write_json(os.path.join(OUT_DIR, "log.json"), {"run": dt.datetime.utcnow().isoformat() + "Z", "lines": LOG[-200:]})
    log("done.")


if __name__ == "__main__":
    main()
