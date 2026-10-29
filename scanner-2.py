#!/usr/bin/env python3
"""
Scanner sinyal IKUT TREN untuk Bitget USDT-M Futures -> Telegram.

Alur:
1. Ambil top-N pair berdasarkan volume 24 jam.
2. Filter tren besar (harian, ~90-180 hari): harga vs EMA200, EMA50 vs EMA200, perubahan 90 hari.
3. Pemicu di 4H, SEARAH tren besar: breakout 20 candle atau pullback ke EMA50.
4. SL berbasis struktur (swing 10 candle) dengan jarak minimal 1.5x ATR. TP 1R/2R/3R (40/40/20).
5. Order book + funding hanya sebagai KONFIRMASI (menambah/mengurangi skor), bukan pemicu.
6. Kirim sinyal terbaik ke Telegram.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.getenv("BITGET_API_URL", "https://api.bitget.com")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = int(os.getenv("TOP_N", "60"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))
MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "2"))  # maks sinyal per run
ATR_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "8"))  # SL lebih jauh dari ini -> dilewati
SEND_EMPTY = os.getenv("SEND_EMPTY", "0") == "1"  # kirim ringkasan walau tidak ada sinyal
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"  # True = cetak saja, tidak kirim Telegram

STABLES = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR"}
TP_SPLIT = (40, 40, 20)
TP_R = (1.0, 2.0, 3.0)
WIB = timezone(timedelta(hours=7))


# ---------------------------------------------------------------- HTTP (Bitget API v2)
PRODUCT = "USDT-FUTURES"
MIX = "/api/v2/mix/market"
GRAN = {"1d": ("1D", 86400000), "4h": ("4H", 14400000)}
TICKERS = {}  # diisi universe(): data ticker + funding rate per simbol


def get(path, **params):
    last = None
    for i in range(3):
        try:
            r = requests.get(BASE + path, params=params, timeout=15)
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


def klines(symbol, interval, limit=250):
    gran, period = GRAN[interval]
    raw = get(f"{MIX}/candles", symbol=symbol, productType=PRODUCT,
              granularity=gran, limit=limit)
    now = int(time.time() * 1000)
    rows = sorted(raw, key=lambda k: int(k[0]))
    # hanya candle yang sudah close; format: [ts, open, high, low, close, volBase, volQuote]
    return [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[6])}
        for k in rows if int(k[0]) + period <= now
    ]


def universe():
    rows = []
    for t in get(f"{MIX}/tickers", productType=PRODUCT):
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym[:-4] in STABLES:
            continue
        TICKERS[sym] = t
        vol = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        rows.append((vol, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:TOP_N]]


# ---------------------------------------------------------------- indikator
def ema(vals, n):
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr(d, n=14):
    trs = []
    for i in range(1, len(d)):
        h, l, pc = d[i]["h"], d[i]["l"], d[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


# ---------------------------------------------------------------- logika sinyal
def big_trend(d1):
    """Tren besar dari candle harian. Return (side, % di atas/bawah EMA200, % perubahan 90 hari)."""
    if len(d1) < 200:  # listing baru -> tidak cukup data
        return None, 0, 0
    closes = [x["c"] for x in d1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    last, ref = closes[-1], closes[-91]
    chg90 = (last / ref - 1) * 100
    dist = (last / e200 - 1) * 100
    if last > e200 and e50 > e200 and last > ref:
        return "LONG", dist, chg90
    if last < e200 and e50 < e200 and last < ref:
        return "SHORT", dist, chg90
    return None, dist, chg90


def trigger(h4, side):
    """Pemicu entry di 4H, harus searah tren besar."""
    if len(h4) < 210:
        return None
    closes = [x["c"] for x in h4]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    a = atr(h4)
    last = h4[-1]
    window = h4[-21:-1]
    prev_high = max(x["h"] for x in window)
    prev_low = min(x["l"] for x in window)
    avg_vol = sum(x["v"] for x in window) / len(window)
    vol_ratio = last["v"] / avg_vol if avg_vol else 0
    entry = last["c"]

    if side == "LONG":
        if e50 <= e200:
            return None
        if entry > prev_high:
            kind = "Breakout 20 candle"
        elif last["l"] <= e50 < entry and entry > last["o"]:
            kind = "Pullback EMA50"
        else:
            return None
        swing = min(x["l"] for x in h4[-10:])
        sl = min(swing - 0.2 * a, entry - ATR_MULT * a)
        ext = (entry - e50) / a
    else:
        if e50 >= e200:
            return None
        if entry < prev_low:
            kind = "Breakdown 20 candle"
        elif last["h"] >= e50 > entry and entry < last["o"]:
            kind = "Pullback EMA50"
        else:
            return None
        swing = max(x["h"] for x in h4[-10:])
        sl = max(swing + 0.2 * a, entry + ATR_MULT * a)
        ext = (e50 - entry) / a

    if ext > 4:  # sudah terlalu jauh dari EMA50 -> mengejar harga
        return None
    risk = abs(entry - sl)
    sl_pct = risk / entry * 100
    if risk <= 0 or sl_pct > MAX_SL_PCT:
        return None
    sign = 1 if side == "LONG" else -1
    tps = [entry + sign * risk * r for r in TP_R]
    return {
        "kind": kind, "entry": entry, "sl": sl, "sl_pct": sl_pct, "tps": tps,
        "vol_ratio": vol_ratio, "ext": ext, "t_close": last["t"] + 4 * 3600 * 1000,
    }


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


def enrich_and_score(sig):
    side = sig["side"]
    score = 4  # lolos tren besar + tren 4H searah
    if sig["kind"].startswith("Break"):
        score += 1
        if sig["vol_ratio"] >= 1.5:
            score += 1
    else:
        score += 1  # pullback: SL lebih dekat ke struktur
    if sig["ext"] <= 2.5:
        score += 1
    if abs(sig["dist200"]) >= 5:
        score += 1

    # konfirmasi order book (bukan pemicu); kalau gagal diambil -> netral
    try:
        r = book_ratio(sig["symbol"])
        conf = r if side == "LONG" else 1 / r
    except (RuntimeError, KeyError, ValueError, TypeError, ZeroDivisionError):
        conf = None
    sig["book"] = conf
    if conf is not None:
        if conf >= 1.15:
            score += 2
        elif conf < 0.87:
            score -= 1

    # funding: terlalu ramai di satu sisi = risiko
    f = funding(sig["symbol"])
    sig["funding"] = f
    if f is not None:
        if (side == "LONG" and f > 0.05) or (side == "SHORT" and f < -0.05):
            score -= 1

    sig["score"] = max(0, min(10, score))
    return sig


# ---------------------------------------------------------------- tampilan Telegram
def fmt(p):
    if p >= 1000:
        return f"{p:.1f}"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.3f}"
    if p >= 0.1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def message(sig):
    long_ = sig["side"] == "LONG"
    emoji = "🟢" if long_ else "🔴"
    entry = sig["entry"]
    name = sig["symbol"]
    stars = "⭐" * round(sig["score"] / 2)
    t_close = datetime.fromtimestamp(sig["t_close"] / 1000, WIB).strftime("%d-%m %H:%M WIB")

    lines = [
        f"{emoji} <b>{sig['side']} • {name}</b>",
        "━━━━━━━━━━━━━━",
        f"⏱ TF 4H • {sig['kind']}",
        f"📈 Tren harian: {'Naik' if long_ else 'Turun'} ({sig['chg90']:+.0f}% / 90 hari) ✅",
        f"{stars} Skor: <b>{sig['score']}/10</b>",
        "",
        f"📍 <b>Entry</b> : <code>{fmt(entry)}</code>",
        f"🛑 <b>SL</b>    : <code>{fmt(sig['sl'])}</code> (-{sig['sl_pct']:.1f}%)",
        "",
    ]
    for i, (tp, share) in enumerate(zip(sig["tps"], TP_SPLIT), 1):
        pct = abs(tp / entry - 1) * 100
        lines.append(f"🎯 <b>TP{i}</b> : <code>{fmt(tp)}</code> ({share}%) +{pct:.1f}%")
    lines += ["", "⚖️ RR 1 : 3 • Risiko maks 1% modal"]

    b = sig["book"]
    if b is None:
        lines.append("📊 Order book: tidak tersedia")
    elif b >= 1.15:
        lines.append(f"📊 Order book: searah ✅ ({b:.2f}x)")
    elif b >= 0.87:
        lines.append(f"📊 Order book: netral ➖ ({b:.2f}x)")
    else:
        lines.append(f"📊 Order book: melawan ⚠️ ({b:.2f}x)")
    if sig["funding"] is not None:
        lines.append(f"💸 Funding: {sig['funding']:+.3f}%")
    if not long_:
        lines.append("⚠️ Short rawan squeeze, disiplin di SL")
    lines += [
        "━━━━━━━━━━━━━━",
        f"🕐 Candle close: {t_close}",
        "<i>Bukan nasihat keuangan. Geser SL ke entry setelah TP1.</i>",
    ]
    return "\n".join(lines)


def send(text):
    if DRY_RUN or not TOKEN or not CHAT_ID:
        print(text)
        print()
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15,
    )
    r.raise_for_status()


# ---------------------------------------------------------------- main
def analyze(sym):
    side, dist, chg90 = big_trend(klines(sym, "1d", 250))
    if not side:
        return None
    sig = trigger(klines(sym, "4h", 250), side)
    if not sig:
        return None
    sig.update(symbol=sym, side=side, dist200=dist, chg90=chg90)
    return sig


def main():
    try:
        symbols = universe()
    except RuntimeError as e:
        send(f"⚠️ Scanner gagal: {e}")
        sys.exit(1)

    candidates, errors = [], 0
    for sym in symbols:
        try:
            sig = analyze(sym)
            if sig:
                candidates.append(sig)
        except RuntimeError:
            errors += 1
        time.sleep(0.15)

    scored = []
    for sig in candidates:
        try:
            scored.append(enrich_and_score(sig))
        except RuntimeError:
            errors += 1
        time.sleep(0.15)

    good = sorted((s for s in scored if s["score"] >= MIN_SCORE),
                  key=lambda s: s["score"], reverse=True)[:MAX_SIGNALS]
    print(f"Scan {len(symbols)} pair | kandidat {len(candidates)} | "
          f"lolos skor {len(good)} | error {errors}")

    for sig in good:
        send(message(sig))
    if not good and SEND_EMPTY:
        send(f"😴 Tidak ada sinyal ikut tren yang memenuhi syarat (kandidat {len(candidates)}, "
             f"skor minimal {MIN_SCORE}). Sabar, jangan paksa entry.")


if __name__ == "__main__":
    main()
