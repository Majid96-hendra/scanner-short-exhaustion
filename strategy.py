"""
Strategi SWING ikut tren (candle harian). Dipakai SAMA PERSIS oleh scanner dan backtest.

Entry (di close candle harian):
  LONG : close tembus high 'entry_n' hari terakhir
         + close > EMA(ema_n) + EMA(50) > EMA(ema_n) + perubahan 'trend_lb' hari > 0
  SHORT: kebalikannya (hanya jika shorts=True)
SL awal : atr_stop x ATR dari entry.
Keluar  : trailing stop = low (long) / high (short) 'exit_n' hari terakhir,
          dinaikkan hanya setelah candle close. Opsional TP parsial di tp_r kali R
          (lalu SL digeser ke entry).
Biaya   : fee taker 2 sisi + slippage 2 sisi + funding (long) dihitung dalam satuan R.
"""

DEFAULT = dict(
    entry_n=20, exit_n=10, ema_n=200, ema_fast=50, trend_lb=90, atr_n=14,
    atr_stop=2.5, max_sl=0.15, shorts=True, tp_r=0.0, tp_frac=0.5,
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


class Ind:
    """Menyimpan (memoize) indikator per simbol supaya grid search cepat."""

    def __init__(self, d):
        self.d = d
        self.c = [x["c"] for x in d]
        self.h = [x["h"] for x in d]
        self.l = [x["l"] for x in d]
        self.m = {}

    def memo(self, key, f):
        if key not in self.m:
            self.m[key] = f()
        return self.m[key]

    def arrays(self, p):
        return {
            "ema_s": self.memo(("ema", p["ema_n"]), lambda: ema(self.c, p["ema_n"])),
            "ema_f": self.memo(("ema", p["ema_fast"]), lambda: ema(self.c, p["ema_fast"])),
            "atr": self.memo(("atr", p["atr_n"]), lambda: atr_series(self.d, p["atr_n"])),
            "hh": self.memo(("hh", p["entry_n"]), lambda: roll(self.h, p["entry_n"], max, True)),
            "ll": self.memo(("ll", p["entry_n"]), lambda: roll(self.l, p["entry_n"], min, True)),
            "xlow": self.memo(("xl", p["exit_n"]), lambda: roll(self.l, p["exit_n"], min, False)),
            "xhigh": self.memo(("xh", p["exit_n"]), lambda: roll(self.h, p["exit_n"], max, False)),
        }


def warmup(p):
    return max(p["ema_n"], p["trend_lb"], p["entry_n"], p["atr_n"]) + 1


# ------------------------------------------------------------------ sinyal
def signal_at(a, ind, i, p):
    """Sinyal di close hari ke-i. Return 'LONG' / 'SHORT' / None."""
    need = (a["ema_s"][i], a["ema_f"][i], a["atr"][i], a["hh"][i], a["ll"][i])
    if any(x is None for x in need) or i - p["trend_lb"] < 0:
        return None
    c = ind.c[i]
    chg = c / ind.c[i - p["trend_lb"]] - 1
    if c > a["hh"][i] and c > a["ema_s"][i] and a["ema_f"][i] > a["ema_s"][i] and chg > 0:
        return "LONG"
    if (p["shorts"] and c < a["ll"][i] and c < a["ema_s"][i]
            and a["ema_f"][i] < a["ema_s"][i] and chg < 0):
        return "SHORT"
    return None


def live_signal(d, p):
    """Cek sinyal pada candle harian terakhir yang sudah close (untuk scanner)."""
    if len(d) < warmup(p) + 1:
        return None
    ind = Ind(d)
    a = ind.arrays(p)
    i = len(d) - 1
    side = signal_at(a, ind, i, p)
    if not side:
        return None
    sgn = 1 if side == "LONG" else -1
    entry = d[i]["c"]
    risk = p["atr_stop"] * a["atr"][i]
    sl_pct = risk / entry
    if risk <= 0 or sl_pct > p["max_sl"]:
        return None
    avg_vol = sum(x["v"] for x in d[i - 20:i]) / 20
    return {
        "side": side, "entry": entry, "sl": entry - sgn * risk, "sl_pct": sl_pct * 100,
        "tp1": entry + sgn * p["tp_r"] * risk if p["tp_r"] > 0 else None,
        "trail": a["xlow"][i] if side == "LONG" else a["xhigh"][i],
        "chg": (entry / ind.c[i - p["trend_lb"]] - 1) * 100,
        "dist200": (entry / a["ema_s"][i] - 1) * 100,
        "vol_ratio": d[i]["v"] / avg_vol if avg_vol else 0,
        "t_close": d[i]["t"] + DAY_MS,
        "cost_r": (2 * FEE + 2 * SLIP) / sl_pct,
    }


# ------------------------------------------------------------------ simulator
def simulate(ind, a, p, sym=""):
    """Simulasi satu simbol. Entry di OPEN hari berikutnya (tanpa lookahead). Hasil dalam R."""
    d = ind.d
    n = len(d)
    trades = []
    i = warmup(p)
    while i < n - 1:
        side = signal_at(a, ind, i, p)
        if not side:
            i += 1
            continue
        sgn = 1 if side == "LONG" else -1
        entry = d[i + 1]["o"] * (1 + sgn * SLIP)
        risk = p["atr_stop"] * a["atr"][i]
        sl_pct = risk / entry
        if risk <= 0 or sl_pct > p["max_sl"]:
            i += 1
            continue
        stop = entry - sgn * risk
        tp = entry + sgn * p["tp_r"] * risk if p["tp_r"] > 0 else None
        parts, rem, j = [], 1.0, i + 1
        while j < n:
            day = d[j]
            if (sgn == 1 and day["l"] <= stop) or (sgn == -1 and day["h"] >= stop):
                fill = min(day["o"], stop) if sgn == 1 else max(day["o"], stop)
                parts.append((rem, fill * (1 - sgn * SLIP), j))
                rem = 0.0
                break
            if tp is not None and ((sgn == 1 and day["h"] >= tp) or (sgn == -1 and day["l"] <= tp)):
                parts.append((p["tp_frac"], tp * (1 - sgn * SLIP), j))
                rem -= p["tp_frac"]
                tp = None
                stop = max(stop, entry) if sgn == 1 else min(stop, entry)
            tr = a["xlow"][j] if sgn == 1 else a["xhigh"][j]
            if tr is not None:
                stop = max(stop, tr) if sgn == 1 else min(stop, tr)
            j += 1
        if rem > 1e-9:  # masih terbuka di akhir data -> tutup di close terakhir
            parts.append((rem, d[n - 1]["c"] * (1 - sgn * SLIP), n - 1))
        last = parts[-1][2]
        gross = sum(f * sgn * (px / entry - 1) for f, px, _ in parts)
        days = sum(f * (k - i) for f, _, k in parts)
        cost = 2 * FEE + (FUND_DAY * days if side == "LONG" else 0.0)
        trades.append({
            "sym": sym, "side": side, "t_entry": d[i + 1]["t"], "t_exit": d[last]["t"],
            "R": (gross - cost) / sl_pct, "sl_pct": sl_pct, "cost_R": cost / sl_pct, "days": days,
        })
        i = max(last, i + 1)
    return trades
