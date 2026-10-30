"""Fungsi bersama: API publik Bitget (USDT-M Futures, v2) dan pengirim Telegram."""
import os
import time

import requests

BASE = os.getenv("BITGET_API_URL", "https://api.bitget.com")
PRODUCT = "USDT-FUTURES"
MIX = "/api/v2/mix/market"
PERIOD_MS = {"1D": 86400000, "4H": 14400000}
STABLES = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR"}
TICKERS = {}  # diisi universe(): data ticker + funding rate per simbol


def get(path, **params):
    last = None
    for i in range(3):
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            if r.status_code in (403, 451):
                raise RuntimeError(
                    f"HTTP {r.status_code}: Bitget memblokir IP/lokasi server ini. "
                    "Jalankan dari VPS/PC lain."
                )
            if r.status_code == 429:
                last = RuntimeError("rate limit")
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "00000":
                raise RuntimeError(f"Bitget error {j.get('code')}: {j.get('msg')}")
            return j["data"]
        except requests.RequestException as e:
            last = e
            time.sleep(1)
    raise RuntimeError(f"Gagal request {path}: {last}")


def candles(symbol, gran="1D", limit=400):
    """Candle yang SUDAH close, urut lama -> baru. Format Bitget: [ts,o,h,l,c,volBase,volQuote]."""
    try:
        raw = get(f"{MIX}/candles", symbol=symbol, productType=PRODUCT,
                  granularity=gran, limit=limit)
    except RuntimeError:
        if limit <= 200:
            raise
        raw = get(f"{MIX}/candles", symbol=symbol, productType=PRODUCT,
                  granularity=gran, limit=200)
    now = int(time.time() * 1000)
    period = PERIOD_MS[gran]
    rows = sorted(raw, key=lambda k: int(k[0]))
    return [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
        for k in rows if int(k[0]) + period <= now
    ]


def universe(top_n):
    rows = []
    for t in get(f"{MIX}/tickers", productType=PRODUCT):
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym[:-4] in STABLES:
            continue
        TICKERS[sym] = t
        vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        rows.append((vol, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top_n]]


def book_ratio(symbol):
    """Rasio bid/ask (nilai USDT) dalam jangkauan yang sama di kedua sisi."""
    d = get(f"{MIX}/merge-depth", symbol=symbol, productType=PRODUCT, limit="max")
    bids = sorted(((float(p), float(q)) for p, q in d["bids"]), reverse=True)
    asks = sorted((float(p), float(q)) for p, q in d["asks"])
    if not bids or not asks:
        return 1.0
    mid = (bids[0][0] + asks[0][0]) / 2
    reach = min(mid - bids[-1][0], asks[-1][0] - mid)
    bid_val = sum(p * q for p, q in bids if p >= mid - reach)
    ask_val = sum(p * q for p, q in asks if p <= mid + reach)
    return bid_val / ask_val if ask_val else 1.0


def funding(symbol):
    v = TICKERS.get(symbol, {}).get("fundingRate")
    return float(v) * 100 if v not in (None, "") else None


def send(text, parse_mode="HTML"):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if os.getenv("DRY_RUN", "0") == "1" or not token or not chat:
        print(text)
        print()
        return
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=20)
    r.raise_for_status()
