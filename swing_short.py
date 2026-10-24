#!/usr/bin/env python3
"""
Scanner Short Swing (RSI jenuh beli lalu berbalik)
--------------------------------------------------
Versi perbaikan dari scanner reversal short. Timeframe default 4 jam (swing).

Pip: ccxt pandas requests
Token dan chat id dari environment variable (jangan ditulis di kode):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Pengaturan opsional (environment variable):
  EXCHANGE=bitget      bitget | binance | bybit | okx   (GitHub Actions di AS: pakai bitget)
  TIMEFRAME=4h         4h (swing) | 1h
  MIN_VOL_USDT=5000000 volume 24 jam minimal
  TOP_N=150            jumlah koin terlikuid yang dicek
  RSI_PEAK=70          RSI harus pernah mencapai ini dalam 6 candle terakhir
  MIN_SCORE=60         skor minimal sinyal (0-100)
  MAX_DROP=10          lewati kalau harga sudah turun lebih dari ini (%) dari puncak
  MIN_RISE=8           harga harus pernah naik minimal ini (%) sebelum puncak (swing butuh kenaikan dulu)
  MAX_SIGNALS=8        maksimal sinyal per scan
  COOLDOWN_HOURS=12    jeda sinyal ulang per koin
  MODAL_USDT=100  RISK_PCT=2   untuk ukuran posisi
  HEARTBEAT=daily      daily | always | off  (laporan walau tidak ada sinyal)

Pakai:
  python3 swing_short.py        # scan sekali, kirim ke Telegram
  python3 swing_short.py test   # kirim pesan uji
"""
import os, sys, json, time, html, logging
from datetime import datetime, timezone

import pandas as pd
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
EXCHANGE = os.getenv("EXCHANGE", "bitget").lower()
TF = os.getenv("TIMEFRAME", "4h")
MIN_VOL = float(os.getenv("MIN_VOL_USDT", "5000000"))
TOP_N = int(os.getenv("TOP_N", "150"))
RSI_PEAK = float(os.getenv("RSI_PEAK", "70"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "60"))
MAX_DROP = float(os.getenv("MAX_DROP", "10"))
MIN_RISE = float(os.getenv("MIN_RISE", "8"))
MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "8"))
COOLDOWN_H = float(os.getenv("COOLDOWN_HOURS", "12"))
MODAL = float(os.getenv("MODAL_USDT", "100"))
RISK_PCT = float(os.getenv("RISK_PCT", "2"))
HEARTBEAT = os.getenv("HEARTBEAT", "daily").lower()
STATE_FILE = os.getenv("STATE_FILE", "swing_state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("swing")


# ---------------- Telegram ----------------
def kirim(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for a in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.ok:
                return True
            log.warning("Telegram menolak (HTTP %s)", r.status_code)
        except Exception as e:
            log.warning("Kirim Telegram gagal (%s)", type(e).__name__)   # tidak mencetak URL yang berisi token
        time.sleep(2 + a * 3)
    return False


# ---------------- Indikator ----------------
def rsi_wilder(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.where(dn != 0)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr_wilder(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd_hist(close, fast=12, slow=26, sig=9):
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return macd - macd.ewm(span=sig, adjust=False).mean()


# ---------------- Analisis satu koin ----------------
def analyze(df):
    """
    stage 0 = tidak relevan, 1 = RSI jenuh beli tapi belum berbalik,
    2 = berbalik tapi belum terkonfirmasi, 3 = sinyal short.
    """
    df = df.iloc[:-1].reset_index(drop=True)          # buang candle yang masih berjalan
    n = len(df)
    if n < 120:
        return None
    c, o, h, l, v = df["close"], df["open"], df["high"], df["low"], df["volume"]
    r = rsi_wilder(c)
    atr = atr_wilder(df).iloc[-1]
    ema21, ema50 = c.ewm(span=21, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    hist = macd_hist(c)
    price, rsi_now, rsi_prev = c.iloc[-1], r.iloc[-1], r.iloc[-2]
    if not atr > 0:
        return None
    W = 6
    rsi_peak = r.iloc[-W:].max()
    base = dict(price=float(price), rsi_now=float(rsi_now), rsi_peak=float(rsi_peak))

    if rsi_peak < RSI_PEAK:
        return dict(base, stage=0)
    if not (rsi_now <= rsi_peak - 3 and rsi_now <= rsi_prev):
        return dict(base, stage=1)                    # masih jenuh beli, belum berbalik
    hi_recent = float(h.iloc[-8:].max())
    drop = (hi_recent - price) / hi_recent * 100
    if rsi_now < 40 or drop > MAX_DROP:
        return dict(base, stage=0, late=True)         # sudah turun jauh, terlambat
    rise = (hi_recent / float(l.iloc[-32:-4].min()) - 1) * 100
    if rise < MIN_RISE:
        return dict(base, stage=0)                    # tidak ada kenaikan berarti sebelum puncak

    pts, tags = 30, ["Naik %.0f%% lalu RSI turun dari %.0f ke %.0f" % (rise, rsi_peak, rsi_now)]
    confirm = 0

    # MACD histogram memotong ke bawah nol dalam 3 candle terakhir
    if any(hist.iloc[-i - 1] >= 0 and hist.iloc[-i] < 0 for i in (1, 2, 3)):
        pts += 20; confirm += 1; tags.append("MACD cross turun")
    # harga turun di bawah EMA21 setelah sempat di atasnya
    if price < ema21.iloc[-1] and (c.iloc[-6:-1] > ema21.iloc[-6:-1]).any():
        pts += 20; confirm += 1; tags.append("Close di bawah EMA21")
    # candle bearish dengan rejection
    rng = h.iloc[-1] - l.iloc[-1]
    if c.iloc[-1] < o.iloc[-1] and rng > 0:
        body = (o.iloc[-1] - c.iloc[-1]) / rng
        wick = (h.iloc[-1] - max(o.iloc[-1], c.iloc[-1])) / rng
        if body >= 0.5 or wick >= 0.4:
            pts += 15; confirm += 1; tags.append("Candle bearish")
    # double top / lower high
    seg = h.iloc[-8:].reset_index(drop=True)
    ip = int(seg.idxmax())
    if ip <= len(seg) - 3:
        h2 = float(seg.iloc[ip + 2:].max())
        if h2 >= hi_recent * 0.97:
            pts += 15; confirm += 1; tags.append("Double top")
        elif h2 >= hi_recent * 0.90:
            pts += 10; confirm += 1; tags.append("Lower high")
    # divergensi: harga high baru tapi RSI lebih rendah
    prior_hi = float(h.iloc[-26:-8].max())
    if hi_recent >= prior_hi * 0.995 and r.iloc[-8:].max() < r.iloc[-26:-8].max() - 3:
        pts += 15; tags.append("Divergensi RSI")
    # volume klimaks di puncak
    avg_v = v.iloc[-38:-8].mean()
    if avg_v > 0 and v.iloc[-8:].max() >= 2 * avg_v:
        pts += 10; tags.append("Volume klimaks")
    # jauh dari EMA50
    ext = (hi_recent / ema50.iloc[-1] - 1) * 100
    if ext >= 8:
        pts += 10; tags.append("Jauh di atas EMA50 (+%.0f%%)" % ext)

    if confirm == 0:
        return dict(base, stage=2, score=pts)

    entry = float(price)
    sl = max(float(h.iloc[-6:].max()), hi_recent) + 0.3 * float(atr)
    sl = max(sl, entry + float(atr))                  # stop minimal 1 ATR
    risk = sl - entry
    if risk / entry > 0.12:
        return dict(base, stage=0, wide=True)
    return dict(base, stage=3, score=min(100, pts), tags=tags, entry=entry, sl=sl, risk=risk,
                tp1=entry - 1.5 * risk, tp2=entry - 3 * risk, min_entry=entry - risk / 3,
                support=float(l.iloc[-60:].min()), drop=float(drop), ts=int(df["timestamp"].iloc[-1]))


# ---------------- Pesan ----------------
def fmt(p):
    return "%.2f" % p if p >= 100 else "%.3f" % p if p >= 1 else "%.5f" % p if p >= 0.01 else "%.3g" % p


def pct(a, b):
    return abs(a / b - 1) * 100


def sinyal_msg(sym, r):
    risk_usdt = MODAL * RISK_PCT / 100
    notional = risk_usdt / (r["risk"] / r["entry"])
    e = r["entry"]
    lines = [
        "🔴 <b>SINYAL SHORT SWING: %s</b>" % html.escape(sym.split("/")[0]),
        "Timeframe %s (skor %d dari 100)" % (TF, round(r["score"])),
        "",
        "Masuk (entry): <b>%s</b>" % fmt(e),
        "Stop loss (SL): <b>%s</b>  (risiko %.1f%%)" % (fmt(r["sl"]), pct(r["sl"], e)),
        "Ambil untung 1 (TP1): <b>%s</b>  (+%.1f%%)" % (fmt(r["tp1"]), pct(r["tp1"], e)),
        "Ambil untung 2 (TP2): <b>%s</b>  (+%.1f%%)" % (fmt(r["tp2"]), pct(r["tp2"], e)),
        "",
        "Ukuran posisi: %.0f USDT (%.1fx), risiko maksimal %.2f USDT" % (notional, notional / MODAL, risk_usdt),
        "Jangan kejar: kalau harga sudah di bawah <b>%s</b>, lewati sinyal ini." % fmt(r["min_entry"]),
    ]
    if r["support"] < r["tp1"]:
        lines.append("Support swing terdekat: %s" % fmt(r["support"]))
    lines += ["", "Alasan: " + html.escape(", ".join(r["tags"])),
              "",
              "Cara pakai: masuk di sekitar harga entry dan pasang SL. Saat TP1 kena, tutup sebagian dan geser SL ke entry."]
    return "\n".join(lines)


# ---------------- State ----------------
def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    cut = time.time() - 3 * 86400
    st["sent"] = {k: v for k, v in st.get("sent", {}).items() if v.get("t", 0) > cut}
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh)


# ---------------- Scan ----------------
def make_exchange():
    import ccxt
    return getattr(ccxt, EXCHANGE)({"enableRateLimit": True, "options": {"defaultType": "swap"}})


def scan(ex, dry=False):
    state = load_state()
    sent = state.setdefault("sent", {})
    now = time.time()

    markets = ex.load_markets()
    perps = [s for s, m in markets.items()
             if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active") is not False]
    tickers = ex.fetch_tickers()
    vol = {s: float((tickers.get(s) or {}).get("quoteVolume") or 0) for s in perps}
    pool = sorted([s for s in perps if vol[s] >= MIN_VOL], key=lambda s: -vol[s])[:TOP_N]

    sinyal, watch, fail = [], [], 0
    funnel = dict(dicek=0, overbought=0, berbalik=0, sinyal=0, terlambat=0)
    for s in pool:
        try:
            raw = ex.fetch_ohlcv(s, TF, limit=250)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            res = analyze(df)
        except Exception:
            fail += 1
            continue
        if not res:
            continue
        funnel["dicek"] += 1
        if res.get("late"):
            funnel["terlambat"] += 1
        if res["rsi_peak"] >= RSI_PEAK:
            funnel["overbought"] += 1
        if res["stage"] >= 2:
            funnel["berbalik"] += 1
        if res["stage"] == 3 and res["score"] >= MIN_SCORE:
            funnel["sinyal"] += 1
            sinyal.append((s, res))
        elif res["stage"] in (1, 2) or res["stage"] == 3:
            watch.append((s, res))
    sinyal.sort(key=lambda x: -x[1]["score"])
    log.info("Funnel: %s | gagal ambil data: %d | koin lolos volume: %d", funnel, fail, len(pool))

    if fail and fail >= len(pool) > 0:
        raise RuntimeError("semua permintaan data gagal")

    terkirim = 0
    for s, r in sinyal:
        k = s
        prev = sent.get(k)
        if prev and (prev.get("ts") == r["ts"] or now - prev.get("t", 0) < COOLDOWN_H * 3600):
            continue
        if terkirim >= MAX_SIGNALS:
            break
        if dry:
            print(sinyal_msg(s, r)); print("-----")
        elif kirim(sinyal_msg(s, r)):
            sent[k] = dict(t=now, ts=r["ts"])
            terkirim += 1
            time.sleep(1)

    # laporan ringkas supaya kelihatan bot hidup
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hb = HEARTBEAT == "always" or (HEARTBEAT == "daily" and state.get("hb_day") != today)
    if hb and not dry:
        watch.sort(key=lambda x: -x[1]["rsi_peak"])
        lines = ["📊 <b>Laporan scan short swing (%s, %s)</b>" % (TF, EXCHANGE),
                 "%d koin dicek. RSI pernah di atas %.0f: %d. Sudah berbalik: %d. Sinyal baru: %d." % (
                     funnel["dicek"], RSI_PEAK, funnel["overbought"], funnel["berbalik"], terkirim)]
        if funnel["terlambat"]:
            lines.append("Dilewati karena sudah turun terlalu jauh: %d." % funnel["terlambat"])
        if watch:
            lines += ["", "Mendekati (RSI jenuh beli, belum lengkap konfirmasi):"]
            for s, r in watch[:6]:
                lines.append("• %s: RSI %.0f (puncak %.0f) %s" % (
                    html.escape(s.split("/")[0]), r["rsi_now"], r["rsi_peak"],
                    "menunggu berbalik" if r["stage"] == 1 else "berbalik, menunggu konfirmasi"))
        if fail:
            lines.append("\nGagal mengambil data: %d koin." % fail)
        kirim("\n".join(lines))
        state["hb_day"] = today
    if not dry:
        save_state(state)
    return terkirim


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if not (TOKEN and CHAT_ID):
        sys.exit("Isi TELEGRAM_TOKEN dan TELEGRAM_CHAT_ID dulu (jangan ditulis di kode).")
    if cmd == "test":
        print("Terkirim" if kirim("Scanner short swing terhubung. Tes berhasil.") else "Gagal mengirim")
        return
    try:
        scan(make_exchange())
    except Exception as e:
        log.error("Scan gagal: %s: %s", type(e).__name__, str(e)[:200])
        state = load_state()
        if time.time() - state.get("err_t", 0) > 6 * 3600:       # jangan spam error
            kirim("⚠️ Scan gagal: %s. Kalau memakai Binance dari GitHub, IP-nya biasanya diblokir; "
                  "pakai EXCHANGE=bitget." % html.escape(type(e).__name__))
            state["err_t"] = time.time()
            save_state(state)
        sys.exit(1)


if __name__ == "__main__":
    main()
