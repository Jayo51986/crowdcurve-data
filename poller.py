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

# ── CrowdCurve models ────────────────────────────────────────────────────────
# Three cheap, honest, fully-explainable models computed from price history.
# Shown as faint lines behind the market consensus and scored publicly.

def fetch_history(gecko_id, days=90, cb_pair=None):
    """Daily closing prices for the last N days. Coinbase candles primary
    (reachable from GitHub runners, keyless), CoinGecko as fallback."""
    # 1. Coinbase daily candles: [time, low, high, open, close, volume]
    if cb_pair:
        end = dt.datetime.utcnow()
        start = end - dt.timedelta(days=days)
        url = ("https://api.exchange.coinbase.com/products/" + cb_pair +
               "/candles?granularity=86400&start=" + start.strftime("%Y-%m-%dT%H:%M:%SZ") +
               "&end=" + end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        d = get_json(url)
        try:
            if isinstance(d, list) and len(d) >= 5:
                # candles come newest-first; sort oldest-first and take close (idx 4)
                rows = sorted(d, key=lambda r: r[0])
                closes = [float(r[4]) for r in rows if len(r) >= 5 and r[4]]
                if len(closes) >= 5:
                    return closes
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    # 2. CoinGecko fallback
    url = ("https://api.coingecko.com/api/v3/coins/" + gecko_id +
           "/market_chart?vs_currency=usd&days=" + str(days) + "&interval=daily")
    d = get_json(url)
    try:
        prices = [p[1] for p in d.get("prices", [])] if d else []
        return [float(x) for x in prices if x]
    except (TypeError, ValueError, KeyError):
        return []


def realized_vol(prices):
    """Annualized volatility and drift from daily log returns."""
    if len(prices) < 5:
        return None
    rets = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            rets.append(math.log(prices[i] / prices[i - 1]))
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365), mean * 365


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def model_probabilities(spot, strikes, sigma_annual, drift_annual, horizon_days, mode):
    """P(price > strike) at the horizon under a log-normal model."""
    if not spot or sigma_annual is None or sigma_annual <= 0:
        return None
    t = max(horizon_days, 1) / 365.0
    sig = sigma_annual * math.sqrt(t)
    if mode == "momentum":
        mu = max(-0.6, min(0.6, drift_annual)) * t / (1 + t)
    else:
        mu = 0.0
    out = []
    for k in strikes:
        if k <= 0:
            out.append(0.0)
            continue
        z = (math.log(k / spot) - (mu - 0.5 * sig * sig)) / (sig or 1e-9)
        out.append(max(0.0, min(1.0, 1 - _norm_cdf(z))))
    return out


def build_models(spot, strikes, market_cons, hist, horizon_days):
    """Return {volatility, momentum, anchored, sigma} or {}."""
    rv = realized_vol(hist)
    if not rv:
        return {}
    sigma, drift = rv
    vol = model_probabilities(spot, strikes, sigma, drift, horizon_days, "vol")
    mom = model_probabilities(spot, strikes, sigma, drift, horizon_days, "momentum")
    if vol is None:
        return {}
    anchored = None
    if market_cons and len(market_cons) == len(vol):
        anchored = []
        for mc, mv in zip(market_cons, vol):
            gap = mv - mc
            adj = mc + (0.25 * gap if abs(gap) > 0.12 else 0.0)
            anchored.append(max(0.0, min(1.0, adj)))
    return {
        "volatility": [round(x, 4) for x in vol],
        "momentum": [round(x, 4) for x in mom],
        "anchored": [round(x, 4) for x in anchored] if anchored else None,
        "sigma": round(sigma, 4),
    }



COINS = {
    "BTC":  {"name": "Bitcoin",     "aliases": ["bitcoin", "btc"],       "cb": "BTC-USD",  "gecko": "bitcoin"},
    "ETH":  {"name": "Ethereum",    "aliases": ["ethereum", "eth"],      "cb": "ETH-USD",  "gecko": "ethereum"},
    "SOL":  {"name": "Solana",      "aliases": ["solana", "sol"],        "cb": "SOL-USD",  "gecko": "solana"},
    "XRP":  {"name": "XRP",         "aliases": ["xrp", "ripple"],        "cb": "XRP-USD",  "gecko": "ripple"},
    "DOGE": {"name": "Dogecoin",    "aliases": ["dogecoin", "doge"],     "cb": "DOGE-USD", "gecko": "dogecoin"},
    "LTC":  {"name": "Litecoin",    "aliases": ["litecoin", "ltc"],      "cb": "LTC-USD",  "gecko": "litecoin"},
    "BNB":  {"name": "BNB",         "aliases": ["bnb", "binance coin"],  "cb": None,       "gecko": "binancecoin"},
    "ADA":  {"name": "Cardano",     "aliases": ["cardano", "ada"],       "cb": "ADA-USD",  "gecko": "cardano"},
    "HBAR": {"name": "Hedera",      "aliases": ["hedera", "hbar"],       "cb": "HBAR-USD", "gecko": "hedera-hashgraph"},
    "ZEC":  {"name": "Zcash",       "aliases": ["zcash", "zec"],         "cb": "ZEC-USD",  "gecko": "zcash"},
    "HYPE": {"name": "Hyperliquid", "aliases": ["hyperliquid", "hype"],  "cb": None,       "gecko": "hyperliquid"},
}

KALSHI_TICKER_HINTS = {
    "KXBTC": "BTC", "BTC": "BTC", "KXETH": "ETH", "ETH": "ETH",
    "KXSOL": "SOL", "SOL": "SOL", "KXXRP": "XRP", "XRP": "XRP",
    "KXDOGE": "DOGE", "DOGE": "DOGE", "KXLTC": "LTC", "LTC": "LTC",
    "KXBNB": "BNB", "KXADA": "ADA", "KXHBAR": "HBAR", "KXZEC": "ZEC",
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
    src = {}
    # 1. Coinbase spot (reachable from GitHub, no key).
    for sym, c in COINS.items():
        if not c.get("cb"):
            continue
        d = get_json("https://api.coinbase.com/v2/prices/" + c["cb"] + "/spot")
        try:
            amt = d["data"]["amount"] if d and isinstance(d.get("data"), dict) else None
            if amt is not None:
                spots[sym] = float(amt)
                src[sym] = "cb"
        except (TypeError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    # 2. Kraken for anything still missing.
    kraken_pairs = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
                    "DOGE": "XDGUSD", "LTC": "LTCUSD", "ADA": "ADAUSD", "HBAR": "HBARUSD",
                    "ZEC": "ZECUSD", "BNB": None, "HYPE": None}
    for sym in [s for s in COINS if s not in spots]:
        pair = kraken_pairs.get(sym)
        if not pair:
            continue
        d = get_json("https://api.kraken.com/0/public/Ticker?pair=" + pair)
        try:
            res = d.get("result") if d else None
            if res:
                first = next(iter(res.values()))
                spots[sym] = float(first["c"][0])  # last trade close
                src[sym] = "kraken"
        except (TypeError, ValueError, KeyError, StopIteration):
            pass
        time.sleep(0.1)
    # 3. CoinGecko last resort (BNB, HYPE, or any gaps).
    missing = [s for s in COINS if s not in spots]
    if missing:
        ids = ",".join(COINS[s]["gecko"] for s in missing)
        d = get_json("https://api.coingecko.com/api/v3/simple/price?ids=" + ids + "&vs_currencies=usd")
        if d:
            for s in missing:
                g = COINS[s]["gecko"]
                if g in d and "usd" in d[g]:
                    spots[s] = float(d[g]["usd"])
                    src[s] = "gecko"
    by_src = {}
    for s, v in src.items():
        by_src[v] = by_src.get(v, 0) + 1
    log("spot sources: " + ", ".join(f"{k}:{v}" for k, v in by_src.items()))
    log("spot prices: " + str(len(spots)) + "/" + str(len(COINS)) + " coins -> " +
        ", ".join(f"{k} {v:,.2f}" for k, v in spots.items()))
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


# ── Deribit (options-implied) ────────────────────────────────────────────────

def fetch_deribit():
    """Convert Deribit's options chain into threshold probabilities, matching the
    {venue, coin, strike, prob, vol, end, q} shape used by the other venues.

    Honest method note: for a European call, the risk-neutral probability that
    the price finishes above the strike is closely tracked by the call's delta.
    Deribit's public ticker returns greeks (delta) per option, so we read the
    delta of each call as the market-implied 'chance price > strike' at expiry.
    This is an approximation (delta also carries a small volatility term), but it
    is a real, options-derived probability, not a model we invented. Deribit's
    public market-data API needs no key.
    """
    out = []
    currencies = {"BTC": "BTC", "ETH": "ETH"}  # Deribit lists options for BTC, ETH
    for sym, cur in currencies.items():
        instruments = get_json(
            "https://www.deribit.com/api/v2/public/get_instruments"
            f"?currency={cur}&kind=option&expired=false"
        )
        if not instruments or "result" not in instruments:
            continue
        # group calls by expiry; we only need calls (delta = P[above strike])
        calls = [i for i in instruments["result"]
                 if i.get("option_type") == "call" and i.get("strike")]
        # limit the number of ticker calls to stay polite on rate limits
        calls = sorted(calls, key=lambda i: (i.get("expiration_timestamp", 0), i.get("strike", 0)))
        seen = 0
        for inst in calls:
            if seen >= 240:  # safety cap per currency
                break
            name = inst.get("instrument_name")
            strike = inst.get("strike")
            exp_ms = inst.get("expiration_timestamp")
            if not name or not strike or not exp_ms:
                continue
            tk = get_json(f"https://www.deribit.com/api/v2/public/ticker?instrument_name={name}")
            seen += 1
            time.sleep(0.15)
            if not tk or "result" not in tk:
                continue
            r = tk["result"]
            greeks = r.get("greeks") or {}
            delta = greeks.get("delta")
            if delta is None:
                continue
            # call delta ~ P[S_T > K]; clamp to a sane probability
            prob = clamp(float(delta))
            end = dt.datetime.utcfromtimestamp(exp_ms / 1000.0).date()
            # volume proxy: open interest * underlying price (USD-ish notional)
            oi = float(r.get("open_interest") or 0)
            under = float(r.get("underlying_price") or r.get("index_price") or 0)
            vol = oi * under
            out.append({"venue": "d", "coin": sym, "strike": float(strike),
                        "prob": prob, "vol": vol, "end": end,
                        "q": f"{sym} > {int(strike)} call (Deribit {end})"})
    log(f"deribit: {len(out)} option-implied threshold points parsed")
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
    host = None
    for h in ["https://api.elections.kalshi.com/trade-api/v2",
              "https://api.kalshi.com/trade-api/v2"]:
        probe = get_json(h + "/markets?status=open&limit=1")
        if probe and "markets" in probe:
            host = h
            log("kalshi host ok: " + h)
            break
    if not host:
        log("kalshi: no reachable host; skipping")
        return out

    # Discover crypto series, then pull each series' open markets.
    series_tickers = set()
    # a) known/likely crypto price series prefixes
    seeds = ["KXBTCD", "KXBTC", "KXETHD", "KXETH", "KXSOLD", "KXSOL",
             "KXXRPD", "KXXRP", "KXDOGED", "KXDOGE", "KXBTCMAXY", "KXETHMAXY"]
    # b) also discover by listing series in the crypto category
    cat = get_json(host + "/series?category=Crypto&limit=200")
    if cat and isinstance(cat.get("series"), list):
        for s in cat["series"]:
            t = s.get("ticker")
            if t:
                series_tickers.add(t)
    series_tickers.update(seeds)
    log("kalshi: " + str(len(series_tickers)) + " candidate crypto series")

    seen = 0
    dumped = False
    for st in series_tickers:
        cursor = ""
        for _ in range(10):
            url = host + "/markets?series_ticker=" + st + "&status=open&limit=200" + (("&cursor=" + cursor) if cursor else "")
            d = get_json(url)
            if not d or not d.get("markets"):
                break
            for mkt in d["markets"]:
                seen += 1
                if not dumped and seen <= 1:  # one sample, for diagnostics
                    log("KALSHI SAMPLE [" + st + "]: " + json.dumps(mkt)[:1200])
                    dumped = True
                try:
                    sym = kalshi_coin(mkt)
                    if not sym:
                        continue
                    strike, direction = None, None
                    if mkt.get("floor_strike") is not None:
                        strike, direction = float(mkt["floor_strike"]), "above"
                    elif mkt.get("cap_strike") is not None:
                        strike, direction = float(mkt["cap_strike"]), "below"
                    else:
                        text = str(mkt.get("title") or "") + " " + str(mkt.get("yes_sub_title") or "") + " " + str(mkt.get("subtitle") or "")
                        strike, direction = parse_price(text), parse_direction(text)
                    if strike is None or direction is None:
                        continue
                    prob = None
                    def fnum(*keys):
                        for kk in keys:
                            v = mkt.get(kk)
                            if v not in (None, ""):
                                try:
                                    return float(v)
                                except (TypeError, ValueError):
                                    pass
                        return None
                    yb = fnum("yes_bid_dollars", "yes_bid")
                    ya = fnum("yes_ask_dollars", "yes_ask")
                    nb = fnum("no_bid_dollars", "no_bid")
                    na = fnum("no_ask_dollars", "no_ask")
                    if yb is not None and ya is not None and (yb or ya):
                        prob = (yb + ya) / 2.0
                    elif nb is not None and na is not None and (nb or na):
                        prob = 1.0 - (nb + na) / 2.0  # yes = 1 - no
                    else:
                        lp = fnum("last_price_dollars", "previous_price_dollars")
                        if lp is not None:
                            prob = lp
                    if prob is not None and prob > 1.5:  # cents, not dollars
                        prob = prob / 100.0
                    if not prob or prob <= 0:
                        continue
                    if direction == "below":
                        prob = 1.0 - prob
                    end = parse_date(mkt.get("close_time") or mkt.get("expiration_time") or mkt.get("expected_expiration_time"))
                    if end is None:
                        continue
                    vol = fnum("volume", "open_interest_fp", "liquidity_dollars") or 0
                    out.append({"venue": "k", "coin": sym, "strike": strike,
                                "prob": clamp(prob), "vol": vol, "end": end,
                                "q": str(mkt.get("title") or "")})
                except (TypeError, ValueError, KeyError):
                    continue
            cursor = d.get("cursor") or ""
            if not cursor:
                break
            time.sleep(0.3)
        time.sleep(0.2)
    log("kalshi: scanned " + str(seen) + " series markets, kept " + str(len(out)) + " crypto threshold markets")
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
    # Quarter should mean "about a quarter out", not "end of this calendar
    # quarter". If the nearest quarter-end is too close (or collides with the
    # month target), roll to the following quarter so Q is always clearly
    # further out than M.
    qe = quarter_end(today)
    while (qe - today).days < 45 or qe <= me:
        qe = quarter_end(qe + dt.timedelta(days=20))
    return {
        "W":  (today + dt.timedelta(days=7), 4),
        "M":  (me, 7),
        "Q":  (qe, 20),
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


def filter_outlier_strikes(points, spot, horizon_days):
    """Drop strikes implausibly far from spot for the given horizon. A handful
    of moonshot strikes (e.g. $250k when spot is $64k) otherwise stretch the
    price axis and drag the computed distribution. The plausible band widens
    with the horizon: a week stays tight, multi-year allows far more room."""
    if not points or not spot:
        return points
    # scale plausible move with sqrt(time): ~weekly tight, multi-year wide
    import math as _m
    horizon_factor = _m.sqrt(max(horizon_days, 1) / 30.0)  # 1.0 at ~1 month
    # Downside bounded much tighter than upside: thin sub-spot daily strikes must
    # not drag a long-horizon distribution down to an implausible crash level.
    lo_mult = max(0.45, 1 - 0.28 * horizon_factor)
    lo = spot * lo_mult
    hi = spot * (1 + 1.6 * horizon_factor)
    kept = [p for p in points if lo <= p["strike"] <= hi]
    if len(kept) < 3:  # relax only the upper bound on sparse sets
        kept = [p for p in points if p["strike"] >= lo]
    kept = kept if len(kept) >= 3 else points
    # Gap detection: if a small low cluster is separated from the main body by a
    # large empty span (e.g. thin $18-29k strikes far below $90k+ year-end strikes),
    # drop the low cluster. We split at the largest ratio-gap and keep the side
    # holding most of the volume.
    if len(kept) >= 4:
        ks = sorted(kept, key=lambda p: p["strike"])
        best_i, best_ratio = None, 2.2  # only act on >2.2x jumps
        for i in range(1, len(ks)):
            lo_s, hi_s = ks[i - 1]["strike"], ks[i]["strike"]
            if lo_s > 0 and hi_s / lo_s > best_ratio:
                best_ratio = hi_s / lo_s
                best_i = i
        if best_i is not None:
            def vol(seg):
                return sum((p.get("pmVol", 0) + p.get("kVol", 0)) for p in seg)
            low_seg, high_seg = ks[:best_i], ks[best_i:]
            keep_seg = high_seg if vol(high_seg) >= vol(low_seg) else low_seg
            if len(keep_seg) >= 3:
                kept = keep_seg
    return kept


# ── consensus math (same bucket method as the plugin) ────────────────────────

def merge_strikes(points):
    """Group venue points at near-identical strikes into rows of
    {strike, pm, k, d, pmVol, kVol, dVol}, monotonically cleaned."""
    points = sorted(points, key=lambda p: p["strike"])
    rows = []
    for p in points:
        if rows and abs(p["strike"] - rows[-1]["strike"]) / max(rows[-1]["strike"], 1e-9) < 0.005:
            row = rows[-1]
        else:
            row = {"strike": p["strike"], "pm": None, "k": None, "d": None,
                   "pmVol": 0, "kVol": 0, "dVol": 0}
            rows.append(row)
        if p["venue"] == "pm":
            row["pm"] = p["prob"] if row["pm"] is None else (row["pm"] + p["prob"]) / 2
            row["pmVol"] += p["vol"]
        elif p["venue"] == "d":
            row["d"] = p["prob"] if row["d"] is None else (row["d"] + p["prob"]) / 2
            row["dVol"] += p["vol"]
        else:
            row["k"] = p["prob"] if row["k"] is None else (row["k"] + p["prob"]) / 2
            row["kVol"] += p["vol"]
    for row in rows:  # fill missing venues from whatever is present
        present = [x for x in (row["pm"], row["k"], row["d"]) if x is not None]
        fallback = sum(present) / len(present) if present else 0.0
        if row["pm"] is None:
            row["pm"] = fallback
        if row["k"] is None:
            row["k"] = fallback
        if row["d"] is None:
            row["d"] = fallback
    # enforce non-increasing survival as strikes rise (use the 3-venue average)
    prev = 1.0
    for row in rows:
        avg = (row["pm"] + row["k"] + row["d"]) / 3
        if avg > prev:
            scale = prev / avg if avg else 1
            row["pm"] = clamp(row["pm"] * scale)
            row["k"] = clamp(row["k"] * scale)
            row["d"] = clamp(row["d"] * scale)
        prev = (row["pm"] + row["k"] + row["d"]) / 3
    return rows


def summarize(spot, rows):
    strikes = [r["strike"] for r in rows]
    tot = sum(r["pmVol"] + r["kVol"] for r in rows) or 1.0

    def cons(r):
        v = r["pmVol"] + r["kVol"] + r.get("dVol", 0)
        if v > 0:
            return (r["pm"] * r["pmVol"] + r["k"] * r["kVol"] + r["d"] * r.get("dVol", 0)) / v
        return (r["pm"] + r["k"] + r["d"]) / 3

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

def fetch_macro():
    """Live macro markets from Kalshi that move crypto. Falls back to a small
    static set only if the API is unreachable, clearly flagged stale."""
    wanted = [
        ("KXFED", "Interest rates change at the next decision"),
        ("KXCPI", "Inflation comes in higher than expected"),
        ("KXRECSS", "US recession called this year"),
    ]
    out = []
    host = "https://api.elections.kalshi.com/trade-api/v2"
    for series, label in wanted:
        d = get_json(host + "/markets?series_ticker=" + series + "&status=open&limit=1")
        try:
            m = (d or {}).get("markets", [])
            if m:
                yb = float(m[0].get("yes_bid_dollars") or 0)
                ya = float(m[0].get("yes_ask_dollars") or 0)
                p = round((yb + ya) / 2 * 100)
                if p > 0:
                    out.append({"q": label, "p": p, "d": 0, "live": True})
        except (TypeError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    if not out:  # honest fallback, flagged so the plugin can label it
        out = [{"q": "Macro markets warming up", "p": None, "d": 0, "live": False}]
    log("macro: " + str(len([m for m in out if m.get('live')])) + " live markets")
    return out


def update_scoring(config, today):
    """Public, append-only prediction log + calibration scoring.

    For each coin/horizon we record today's stated probability that price is
    above a near-spot reference strike, for every source (Polymarket, Kalshi,
    consensus, and each model). When a prediction's horizon date passes, we
    grade it against the actual spot price and fold it into the calibration
    table. Nothing is ever rewritten: this is the receipts page.
    """
    pred_path = os.path.join(OUT_DIR, "predictions.json")
    score_path = os.path.join(OUT_DIR, "scores.json")
    try:
        with open(pred_path) as f:
            preds = json.load(f)
    except (FileNotFoundError, ValueError):
        preds = []

    targets = horizon_targets(today)
    # 1. log today's open predictions (one per coin/horizon, deduped per day)
    stamp = today.isoformat()
    have = {(p["coin"], p["hk"], p["logged"]) for p in preds}
    for sym, c in config["coins"].items():
        for hk, H in c.get("horizons", {}).items():
            if (sym, hk, stamp) in have:
                continue
            mk = H.get("markets") or []
            if len(mk) < 3:
                continue
            spot = c.get("spot")
            if not spot:
                continue
            # reference strike: nearest to spot
            ref = min(mk, key=lambda m: abs(m["strike"] - spot))
            v = (ref.get("pmVol", 0) + ref.get("kVol", 0)) or 1
            cons = (ref["pm"] * ref.get("pmVol", 0) + ref["k"] * ref.get("kVol", 0)) / v if (ref.get("pmVol", 0) + ref.get("kVol", 0)) else (ref["pm"] + ref["k"]) / 2
            idx = mk.index(ref)
            models = H.get("models") or {}
            def mval(key):
                arr = models.get(key)
                return round(arr[idx], 4) if arr and idx < len(arr) else None
            tgt, _ = targets[hk]
            preds.append({
                "coin": sym, "hk": hk, "logged": stamp,
                "resolve": tgt.isoformat(), "strike": ref["strike"],
                "p": {"pm": ref["pm"], "k": ref["k"], "consensus": round(cons, 4),
                      "volatility": mval("volatility"), "momentum": mval("momentum"),
                      "anchored": mval("anchored")},
                "graded": False,
            })

    # 2. grade matured predictions against today's spot
    spot_now = {s: config["coins"][s].get("spot") for s in config["coins"]}
    bins = ["10-30%", "30-50%", "50-70%", "70-90%"]
    sources = ["pm", "k", "consensus", "volatility", "momentum", "anchored"]
    tally = {s: {b: {"n": 0, "sum_p": 0.0, "hits": 0} for b in bins} for s in sources}

    def binof(p):
        pc = p * 100
        if 10 <= pc < 30: return "10-30%"
        if 30 <= pc < 50: return "30-50%"
        if 50 <= pc < 70: return "50-70%"
        if 70 <= pc < 90: return "70-90%"
        return None

    for p in preds:
        if not p.get("graded") and p["resolve"] <= stamp:
            sp = spot_now.get(p["coin"])
            if sp:
                p["outcome"] = 1 if sp > p["strike"] else 0
                p["graded"] = True
        if p.get("graded") and "outcome" in p:
            for s in sources:
                pr = p["p"].get(s)
                if pr is None:
                    continue
                b = binof(pr)
                if not b:
                    continue
                tally[s][b]["n"] += 1
                tally[s][b]["sum_p"] += pr
                tally[s][b]["hits"] += p["outcome"]

    # 3. build the scorecard: mean calibration error per source
    scorecard = {"resolved": sum(1 for p in preds if p.get("graded")),
                 "open": sum(1 for p in preds if not p.get("graded")),
                 "sources": {}}
    for s in sources:
        rows, errs = [], []
        for b in bins:
            t = tally[s][b]
            if t["n"] == 0:
                continue
            said = t["sum_p"] / t["n"] * 100
            happened = t["hits"] / t["n"] * 100
            rows.append({"bin": b, "n": t["n"], "said": round(said, 1), "happened": round(happened, 1)})
            errs.append(abs(said - happened))
        scorecard["sources"][s] = {
            "rows": rows,
            "mce": round(sum(errs) / len(errs), 1) if errs else None,
        }

    # keep the log bounded (most recent 5000 predictions)
    preds = preds[-5000:]
    write_json(pred_path, preds)
    write_json(score_path, scorecard)
    config["scorecard"] = scorecard
    log("scoring: " + str(scorecard["resolved"]) + " resolved, " + str(scorecard["open"]) + " open")


def main():
    today = dt.datetime.utcnow().date()
    targets = horizon_targets(today)
    spots = fetch_spots()
    if not spots:
        log("FATAL: no spot prices available; aborting without writing")
        sys.exit(1)

    raw = fetch_polymarket() + fetch_kalshi() + fetch_deribit()
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
    # Dynamic horizon labels so the year tabs roll forward forever with no code edits.
    config["horizon_labels"] = {
        "W": "week", "M": "month", "Q": "quarter", "Y": "year-end",
        "Y1": str(today.year + 1), "Y2": str(today.year + 2),
    }

    horizon_keys = ["W", "M", "Q", "Y", "Y1", "Y2"]
    HDAYS = {"W": 7, "M": 30, "Q": 100, "Y": 200, "Y1": 565, "Y2": 930}
    for sym, c in COINS.items():
        name = c["name"]
        spot = spots.get(sym)
        coin_out = {"name": name, "spot": spot, "horizons": {}}
        valid = []
        # price history once per coin, reused for every horizon's models
        hist = fetch_history(c["gecko"], 90, c.get("cb")) if spot else []
        time.sleep(0.4)  # be gentle on the free history endpoint
        for hk in horizon_keys:
            target, _tol = targets[hk]
            label = target.strftime("%b %-d, %Y") if os.name != "nt" else target.strftime("%b %d, %Y")
            pts = grouped.get(sym, {}).get(hk, [])
            pts = filter_outlier_strikes(pts, spot, HDAYS.get(hk, 30))
            rows = merge_strikes(pts) if pts else []
            if spot and len(rows) >= 3:
                label = target.strftime("%b %d, %Y").replace(" 0", " ")
                summary = summarize(spot, rows)
                strikes = [r["strike"] for r in rows]
                market_cons = []
                for r in rows:
                    v = r["pmVol"] + r["kVol"] + r.get("dVol", 0)
                    market_cons.append((r["pm"] * r["pmVol"] + r["k"] * r["kVol"] + r["d"] * r.get("dVol", 0)) / v if v > 0 else (r["pm"] + r["k"] + r["d"]) / 3)
                models = build_models(spot, strikes, market_cons, hist, HDAYS.get(hk, 30)) if hist else {}
                coin_out["horizons"][hk] = {
                    "resolve": label,
                    "markets": [{"strike": r["strike"], "pm": round(r["pm"], 4), "k": round(r["k"], 4),
                                 "d": round(r["d"], 4),
                                 "pmVol": round(r["pmVol"]), "kVol": round(r["kVol"]),
                                 "dVol": round(r.get("dVol", 0))} for r in rows],
                    "summary": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()},
                    "models": models,
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

    # live macro markets from Kalshi (replaces the hardcoded placeholders)
    config["macro"] = fetch_macro()
    # prediction logging + scoring (calibration scorecard + signal feed inputs)
    try:
        update_scoring(config, today)
    except Exception as e:  # never let scoring break the main data write
        log("scoring skipped: " + str(e))

    write_json(os.path.join(OUT_DIR, "config.json"), config)
    # daily history snapshot: latest run of each UTC day wins
    write_json(os.path.join(OUT_DIR, "history", today.isoformat() + ".json"), config)
    write_json(os.path.join(OUT_DIR, "log.json"), {"run": dt.datetime.utcnow().isoformat() + "Z", "lines": LOG[-200:]})
    log("done.")


if __name__ == "__main__":
    main()
