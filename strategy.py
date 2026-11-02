"""
Strategi ikut tren (candle harian ATAU 4 jam). Dipakai SAMA PERSIS oleh scanner dan backtest.
Mode: breakout, pullback (ke EMA lalu lanjut), cross. Keluar: trailing, TP, atau keluar paksa (max_hold).

Entry (di close candle harian):
  LONG : close tembus high 'entry_n' hari terakhir
         + close > EMA(ema_n) + EMA(50) > EMA(ema_n) + perubahan 'trend_lb' hari > 0
  SHORT: kebalikannya (hanya jika shorts=True)
SL awal : atr_stop x ATR dari entry.
Mode "cross": LONG saat EMA(ema_fast) baru cross naik di atas EMA(ema_n) (maks cross_n hari lalu)
          dan harga di atas kedua garis + perubahan trend_lb hari > 0 (SHORT kebalikannya).
Keluar  : trailing stop = low (long) / high (short) 'exit_n' hari terakhir,
          dinaikkan hanya setelah candle close. Opsional TP parsial di tp_r kali R
          (lalu SL digeser ke entry).
Biaya   : fee taker 2 sisi + slippage 2 sisi + funding (long) dihitung dalam satuan R.
"""

from bisect import bisect_right

DEFAULT = dict(
    entry_n=20, exit_n=10, ema_n=200, ema_fast=50, trend_lb=90, atr_n=14,
    atr_stop=2.5, max_sl=0.15, shorts=False, tp_r=0.0, tp_frac=0.5,
    mode="breakout", cross_n=20, tp_pct=0.0,
    btc=False, rs_top=0.0,   # filter pasar: BTC > EMA200; momentum 90 hari top X%
    pb_n=20,                 # mode pullback: EMA tempat harga 'turun lalu naik lagi'
    max_hold=0,              # keluar paksa setelah N candle (0 = mati)
    trail=True,              # trailing stop Donchian aktif
    edge=False,              # True: hanya sinyal PERTAMA (bukan tiap candle berturut-turut)
    bar_ms=86400000,         # panjang satu candle (ms): harian
)
# Profil 4 jam: angka dikali 6 dari versi harian supaya tren besarnya tetap 90-100 hari
PROFILE_4H = dict(
    bar_ms=14400000, ema_n=600, ema_fast=300, trend_lb=540, atr_n=14, max_sl=0.08,
    shorts=False, tp_frac=1.0, trail=False, edge=True,
)
FEE = 0.0006       # fee taker per sisi (0,06%)
SLIP = 0.0005      # slippage per sisi (0,05%)
FUND_DAY = 0.0003  # funding ~0,01% per 8 jam, ditanggung posisi LONG
DAY_MS = 86400000


# ------------------------------------------------------------------ indikator
def ema(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr_series(d, n):
    L = len(d)
    out = [None] * L
    if L <= n:
        return out
    trs = [None] + [
        max(d[i]["h"] - d[i]["l"], abs(d[i]["h"] - d[i - 1]["c"]), abs(d[i]["l"] - d[i - 1]["c"]))
        for i in range(1, L)
    ]
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, L):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def roll(vals, n, fn, prior):
    """fn=max/min atas jendela n. prior=True: jendela SEBELUM hari i (tanpa hari i)."""
    out = [None] * len(vals)
    for i in range(len(vals)):
        lo = i - n if prior else i - n + 1
        hi = i if prior else i + 1
        if lo >= 0:
            out[i] = fn(vals[lo:hi])
    return out


def last_cross(ef, es, up):
    """Indeks cross terakhir (<= i) antara EMA cepat dan lambat; None bila belum ada."""
    out, last = [None] * len(ef), None
    for i in range(1, len(ef)):
        if None not in (ef[i], es[i], ef[i - 1], es[i - 1]):
            if up and ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
                last = i
            elif not up and ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
                last = i
        out[i] = last
    return out


class Ind:
    """Menyimpan (memoize) indikator per simbol supaya grid search cepat."""

    def __init__(self, d):
        self.d = d
        self.c = [x["c"] for x in d]
        self.h = [x["h"] for x in d]
        self.o = [x["o"] for x in d]
        self.l = [x["l"] for x in d]
        self.m = {}
        self.ctx = None   # konteks pasar (BTC & peringkat momentum), diisi dari luar

    def memo(self, key, f):
        if key not in self.m:
            self.m[key] = f()
        return self.m[key]

    def arrays(self, p):
        ef = self.memo(("ema", p["ema_fast"]), lambda: ema(self.c, p["ema_fast"]))
        es = self.memo(("ema", p["ema_n"]), lambda: ema(self.c, p["ema_n"]))
        return {
            "ema_s": es,
            "ema_f": ef,
            "atr": self.memo(("atr", p["atr_n"]), lambda: atr_series(self.d, p["atr_n"])),
            "hh": self.memo(("hh", p["entry_n"]), lambda: roll(self.h, p["entry_n"], max, True)),
            "ll": self.memo(("ll", p["entry_n"]), lambda: roll(self.l, p["entry_n"], min, True)),
            "xlow": self.memo(("xl", p["exit_n"]), lambda: roll(self.l, p["exit_n"], min, False)),
            "xhigh": self.memo(("xh", p["exit_n"]), lambda: roll(self.h, p["exit_n"], max, False)),
            "pb": self.memo(("ema", p["pb_n"]), lambda: ema(self.c, p["pb_n"])),
            "lup": self.memo(("lup", p["ema_fast"], p["ema_n"]), lambda: last_cross(ef, es, True)),
            "ldn": self.memo(("ldn", p["ema_fast"], p["ema_n"]), lambda: last_cross(ef, es, False)),
        }


def warmup(p):
    return max(p["ema_n"], p["trend_lb"], p["entry_n"], p["atr_n"], p["pb_n"]) + 1


# ------------------------------------------------------------------ sinyal
def build_ctx(cs, btc_sym="BTCUSDT", btc_n=200, lb=90):
    """Konteks pasar per tanggal: BTC di atas EMA200? dan daftar return 'lb' hari semua koin."""
    ctx = {"btc_ok": {}, "rs": {}}
    btc = cs.get(btc_sym)
    if btc and len(btc) > btc_n:
        e = ema([x["c"] for x in btc], btc_n)
        for k, x in enumerate(btc):
            if e[k] is not None:
                ctx["btc_ok"][x["t"]] = x["c"] > e[k]
    rets = {}
    for d in cs.values():
        for k in range(lb, len(d)):
            rets.setdefault(d[k]["t"], []).append(d[k]["c"] / d[k - lb]["c"] - 1)
    ctx["rs"] = {t: sorted(v) for t, v in rets.items()}
    return ctx


def ctx_args(p):
    """Parameter konteks pasar yang setara 'EMA200 harian' dan 'momentum 90 hari' untuk profil p."""
    return {"btc_n": int(200 * DAY_MS / p["bar_ms"]), "lb": p["trend_lb"]}


def ctx_ok(ind, i, p):
    """Filter pasar untuk LONG. Tanpa konteks (ind.ctx None) atau filter mati -> lolos."""
    ctx = ind.ctx
    if ctx is None:
        return True
    t = ind.d[i]["t"]
    if p.get("btc") and not ctx["btc_ok"].get(t, False):
        return False
    if p.get("rs_top", 0) > 0:
        lst = ctx["rs"].get(t)
        if not lst or i - p["trend_lb"] < 0:
            return False
        mine = ind.c[i] / ind.c[i - p["trend_lb"]] - 1
        if (len(lst) - bisect_right(lst, mine)) / len(lst) >= p["rs_top"]:
            return False
    return True


def cross_state(a, ind, i, p):
    """Kondisi mode 'cross' pada hari i (bukan kejadian): LONG / SHORT / None."""
    ef, es = a["ema_f"][i], a["ema_s"][i]
    if ef is None or es is None or i - p["trend_lb"] < 0:
        return None
    c = ind.c[i]
    chg = c / ind.c[i - p["trend_lb"]] - 1
    lu, ld = a["lup"][i], a["ldn"][i]
    if ef > es and c > ef and c > es and chg > 0 and lu is not None and i - lu < p["cross_n"]:
        return "LONG"
    if (p["shorts"] and ef < es and c < ef and c < es and chg < 0
            and ld is not None and i - ld < p["cross_n"]):
        return "SHORT"
    return None


def signal_at(a, ind, i, p):
    """Sinyal di close hari ke-i. Return 'LONG' / 'SHORT' / None."""
    if p["mode"] == "pullback":
        pb = a["pb"][i]
        if (any(x is None for x in (a["ema_s"][i], a["ema_f"][i], a["atr"][i], pb))
                or i - p["trend_lb"] < 0):
            return None
        c = ind.c[i]
        chg = c / ind.c[i - p["trend_lb"]] - 1
        if (ind.l[i] <= pb < c and c > ind.o[i] and c > a["ema_s"][i]
                and a["ema_f"][i] > a["ema_s"][i] and chg > 0):
            return "LONG" if ctx_ok(ind, i, p) else None
        if (p["shorts"] and ind.h[i] >= pb > c and c < ind.o[i] and c < a["ema_s"][i]
                and a["ema_f"][i] < a["ema_s"][i] and chg < 0):
            return "SHORT"
        return None
    if p["mode"] == "cross":
        if i < 1 or a["atr"][i] is None:
            return None
        side = cross_state(a, ind, i, p)
        # sinyal hanya pada hari kondisi PERTAMA kali terpenuhi (bukan tiap hari)
        if not side or cross_state(a, ind, i - 1, p) == side:
            return None
        return None if side == "LONG" and not ctx_ok(ind, i, p) else side
    need = (a["ema_s"][i], a["ema_f"][i], a["atr"][i], a["hh"][i], a["ll"][i])
    if any(x is None for x in need) or i - p["trend_lb"] < 0:
        return None
    c = ind.c[i]
    chg = c / ind.c[i - p["trend_lb"]] - 1
    if c > a["hh"][i] and c > a["ema_s"][i] and a["ema_f"][i] > a["ema_s"][i] and chg > 0:
        return "LONG" if ctx_ok(ind, i, p) else None
    if (p["shorts"] and c < a["ll"][i] and c < a["ema_s"][i]
            and a["ema_f"][i] < a["ema_s"][i] and chg < 0):
        return "SHORT"
    return None


def sig_at(a, ind, i, p):
    """signal_at + (opsional) hanya sinyal pertama dari rangkaian candle berturut-turut."""
    side = signal_at(a, ind, i, p)
    if side and p.get("edge") and i >= 1 and signal_at(a, ind, i - 1, p) == side:
        return None
    return side


def signal_list(ind, a, p):
    """Daftar (indeks, arah) semua sinyal pada satu simbol; di-cache karena tidak bergantung SL/TP."""
    key = ("sig", p["mode"], p["entry_n"], p["pb_n"], p["ema_n"], p["ema_fast"], p["trend_lb"],
           p["atr_n"], p["shorts"], p["btc"], p["rs_top"], p["cross_n"], p["edge"])

    def build():
        out = []
        for k in range(warmup(p), len(ind.d) - 1):
            side = sig_at(a, ind, k, p)
            if side:
                out.append((k, side))
        return out
    return ind.memo(key, build)


def live_signal(d, p, ctx=None):
    """Cek sinyal pada candle harian terakhir yang sudah close (untuk scanner)."""
    if len(d) < warmup(p) + 1:
        return None
    ind = Ind(d)
    ind.ctx = ctx
    a = ind.arrays(p)
    i = len(d) - 1
    side = sig_at(a, ind, i, p)
    if not side:
        return None
    sgn = 1 if side == "LONG" else -1
    entry = d[i]["c"]
    risk = p["atr_stop"] * a["atr"][i]
    sl_pct = risk / entry
    if risk <= 0 or sl_pct > p["max_sl"]:
        return None
    avg_vol = sum(x["v"] for x in d[i - 20:i]) / 20
    lst = ctx["rs"].get(d[i]["t"]) if ctx else None
    rs_top_pct = None
    if lst:
        mine = entry / ind.c[i - p["trend_lb"]] - 1
        rs_top_pct = (len(lst) - bisect_right(lst, mine)) / len(lst) * 100
    return {
        "side": side, "entry": entry, "sl": entry - sgn * risk, "sl_pct": sl_pct * 100,
        "tp1": (entry * (1 + sgn * p["tp_pct"]) if p["tp_pct"] > 0
                else entry + sgn * p["tp_r"] * risk if p["tp_r"] > 0 else None),
        "tp_full": p["tp_pct"] > 0 or (p["tp_r"] > 0 and p["tp_frac"] >= 1.0),
        "max_hold": p["max_hold"], "hold_hours": p["max_hold"] * p["bar_ms"] / 3600000,
        "trail": a["xlow"][i] if side == "LONG" else a["xhigh"][i],
        "chg": (entry / ind.c[i - p["trend_lb"]] - 1) * 100,
        "dist200": (entry / a["ema_s"][i] - 1) * 100,
        "vol_ratio": d[i]["v"] / avg_vol if avg_vol else 0,
        "t_close": d[i]["t"] + p["bar_ms"],
        "cost_r": (2 * FEE + 2 * SLIP) / sl_pct,
        "rs_top_pct": rs_top_pct, "rs_n": len(lst) if lst else 0,
    }


def status(d, p):
    """Ringkasan kondisi hari ini (untuk laporan harian): lolos filter tren? seberapa dekat ke breakout?"""
    if len(d) < warmup(p) + 1:
        return None
    ind = Ind(d)
    a = ind.arrays(p)
    i = len(d) - 1
    if any(x is None for x in (a["ema_s"][i], a["ema_f"][i], a["hh"][i], a["ll"][i])):
        return None
    c = ind.c[i]
    chg = c / ind.c[i - p["trend_lb"]] - 1
    return {
        "up": c > a["ema_s"][i] and a["ema_f"][i] > a["ema_s"][i] and chg > 0,
        "dn": c < a["ema_s"][i] and a["ema_f"][i] < a["ema_s"][i] and chg < 0,
        "to_high": (a["hh"][i] / c - 1) * 100,   # >0: masih di bawah level breakout
        # >0: EMA cepat masih di bawah EMA lambat; 'approach' = harga sudah di atas EMA cepat
        "gap": (a["ema_s"][i] / a["ema_f"][i] - 1) * 100,
        "approach": c > a["ema_f"][i] and chg > 0,
        "t_close": d[i]["t"] + p["bar_ms"],
    }


# ------------------------------------------------------------------ simulator
def simulate(ind, a, p, sym=""):
    """Simulasi satu simbol. Entry di OPEN candle berikutnya (tanpa lookahead). Hasil dalam R."""
    d = ind.d
    n = len(d)
    trades = []
    busy = 0                       # sinyal baru baru boleh diambil setelah posisi sebelumnya keluar
    bar_days = p["bar_ms"] / DAY_MS
    for i, side in signal_list(ind, a, p):
        if i < busy:
            continue
        sgn = 1 if side == "LONG" else -1
        entry = d[i + 1]["o"] * (1 + sgn * SLIP)
        risk = p["atr_stop"] * a["atr"][i]
        sl_pct = risk / entry
        if risk <= 0 or sl_pct > p["max_sl"]:
            continue
        stop = entry - sgn * risk
        tp = entry + sgn * p["tp_r"] * risk if p["tp_r"] > 0 else None
        tp_full = entry * (1 + sgn * p["tp_pct"]) if p["tp_pct"] > 0 else None
        parts, rem, j = [], 1.0, i + 1
        while j < n:
            day = d[j]
            if (sgn == 1 and day["l"] <= stop) or (sgn == -1 and day["h"] >= stop):
                fill = min(day["o"], stop) if sgn == 1 else max(day["o"], stop)
                parts.append((rem, fill * (1 - sgn * SLIP), j))
                rem = 0.0
                break
            if tp_full is not None and ((sgn == 1 and day["h"] >= tp_full)
                                        or (sgn == -1 and day["l"] <= tp_full)):
                parts.append((rem, tp_full * (1 - sgn * SLIP), j))
                rem = 0.0
                break
            if tp is not None and ((sgn == 1 and day["h"] >= tp) or (sgn == -1 and day["l"] <= tp)):
                parts.append((p["tp_frac"], tp * (1 - sgn * SLIP), j))
                rem -= p["tp_frac"]
                tp = None
                if rem <= 1e-9:    # TP penuh (tp_frac = 1)
                    rem = 0.0
                    break
                stop = max(stop, entry) if sgn == 1 else min(stop, entry)
            if p["max_hold"] and j - i >= p["max_hold"]:   # keluar paksa di close candle ini
                parts.append((rem, day["c"] * (1 - sgn * SLIP), j))
                rem = 0.0
                break
            if p["trail"]:
                tr = a["xlow"][j] if sgn == 1 else a["xhigh"][j]
                if tr is not None:
                    stop = max(stop, tr) if sgn == 1 else min(stop, tr)
            j += 1
        if rem > 1e-9:  # masih terbuka di akhir data -> tutup di close terakhir
            parts.append((rem, d[n - 1]["c"] * (1 - sgn * SLIP), n - 1))
        last = parts[-1][2]
        gross = sum(f * sgn * (px / entry - 1) for f, px, _ in parts)
        days = sum(f * (k - i) for f, _, k in parts) * bar_days
        cost = 2 * FEE + (FUND_DAY * days if side == "LONG" else 0.0)
        trades.append({
            "sym": sym, "side": side, "t_entry": d[i + 1]["t"], "t_exit": d[last]["t"],
            "R": (gross - cost) / sl_pct, "sl_pct": sl_pct, "cost_R": cost / sl_pct, "days": days,
        })
        busy = max(last, i + 1)
    return trades
