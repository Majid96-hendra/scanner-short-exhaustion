#!/usr/bin/env python3
"""
Backtest + pencarian parameter strategi swing (harian) dengan biaya nyata.

Pemakaian:  python backtest.py all      (ambil data lalu backtest)
            python backtest.py fetch    (hanya ambil data -> data.json)
            python backtest.py run      (hanya backtest dari data.json)

Cara kerja:
- Data dibagi waktu: 70% awal (in-sample) untuk MEMILIH parameter,
  30% akhir (out-of-sample) hanya untuk MENGUJI. Hanya hasil out-of-sample yang layak dipercaya.
- Batas posisi bersamaan (MAX_POS=2) diterapkan seperti trading sungguhan.
- Hasil: hasil_backtest.txt, params.json (dibaca scanner), ringkasan dikirim ke Telegram.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from itertools import product

import common
import strategy
from strategy import DEFAULT, Ind, simulate

TF = os.getenv("TF", "4H")                      # "4H" = trader harian (cepat), "1D" = swing
BASE = strategy.PROFILE_4H if TF == "4H" else {}
BAR_MS = BASE.get("bar_ms", strategy.DAY_MS)
BT_DAYS = int(os.getenv("BT_DAYS", "720" if TF == "4H" else "1000"))
DATA_FILE = os.getenv("DATA_FILE", f"data_{TF.lower()}.json")
TOP_N = int(os.getenv("BT_TOP_N", "100"))  # makin banyak pair = makin banyak trade untuk diuji
MAX_POS = int(os.getenv("MAX_POS", "2"))
IS_FRAC = 0.7
MIN_TRADES = 30
# syarat lolos uji akhir: harian = sedikit trade tapi edge harus besar; 4 jam = banyak trade, edge kecil cukup
VERIFY = dict(n=30, avg=0.2, pf=1.3) if TF == "1D" else dict(n=150, avg=0.10, pf=1.15)
if TF == "4H":
    ENTRIES = [
        dict(mode="breakout", entry_n=12),   # tembus high 12 candle 4J (~2 hari)
        dict(mode="breakout", entry_n=24),   # ~4 hari
        dict(mode="pullback", pb_n=20),      # turun ke EMA20 lalu naik lagi
        dict(mode="pullback", pb_n=50),
    ]
    TPS = [dict(tp_r=1.0), dict(tp_r=1.5), dict(tp_r=2.0)]   # TP penuh (tp_frac=1 dari BASE)
    GRID = {
        "atr_stop": [1.5, 2.0],
        "max_hold": [6, 12],           # keluar paksa setelah 24 jam / 48 jam
        "btc": [False, True],          # long hanya jika BTC di atas EMA200 (setara harian)
        "rs_top": [0.0],
    }
else:
    ENTRIES = [
        dict(mode="breakout", entry_n=20, exit_n=20, ema_n=100, shorts=False),
    ]
    TPS = [
        dict(tp_r=0.0, tp_pct=0.0),   # tanpa TP tetap (trailing)
        dict(tp_r=3.0, tp_pct=0.0),   # TP parsial 3R
    ]
    GRID = {
        "atr_stop": [2.0, 3.0],
        "btc": [False, True],          # long hanya jika BTC di atas EMA200
        "rs_top": [0.0, 0.3, 0.5],     # hanya koin dengan momentum 90 hari top 30% / 50%
    }


def entry_label(e):
    if e["mode"] == "pullback":
        return f"Pullback ke EMA{e['pb_n']}"
    return f"Breakout {e['entry_n']} candle"


def tp_label(p):
    if p["tp_pct"] > 0:
        return f"TP penuh +{p['tp_pct'] * 100:.0f}%"
    if p["tp_r"] > 0 and p["tp_frac"] >= 1.0:
        return f"TP penuh {p['tp_r']:g}R"
    if p["tp_r"] > 0:
        return f"TP parsial {p['tp_r']:g}R (50%)"
    return "tanpa TP tetap (trailing)"


# ---------------------------------------------------------------- data
def fetch():
    syms = common.universe(TOP_N)
    if "BTCUSDT" not in syms:
        syms.append("BTCUSDT")   # dibutuhkan untuk filter pasar
    data, lens, errs = {}, [], []
    for s in syms:
        try:
            cs, err = common.candles_history(s, TF, int(BT_DAYS * strategy.DAY_MS / BAR_MS))
            if err:
                errs.append(err)
            data[s] = [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in cs]
            lens.append(len(cs))
        except RuntimeError as e:
            errs.append(str(e))
            print("lewati", s, e)
        time.sleep(0.12)
    with open(DATA_FILE, "w") as f:
        json.dump({"symbols": list(data), "candles": data}, f)
    lens.sort()
    if lens:
        print(f"Data tersimpan: {len(data)} pair | hari per pair: "
              f"min {lens[0]}, median {lens[len(lens) // 2]}, maks {lens[-1]}")
    if errs:
        print("Pesan error pertama:", errs[0])


def load():
    with open(DATA_FILE) as f:
        j = json.load(f)
    return {
        s: [dict(t=int(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in j["candles"][s]]
        for s in j["symbols"]
    }


# ---------------------------------------------------------------- statistik
def portfolio(trades, rank):
    """Terapkan batas MAX_POS posisi bersamaan (urut waktu, lalu peringkat volume)."""
    out, opens = [], []
    for t in sorted(trades, key=lambda t: (t["t_entry"], rank[t["sym"]])):
        opens = [x for x in opens if x > t["t_entry"]]
        if len(opens) < MAX_POS:
            out.append(t)
            opens.append(t["t_exit"])
    return out


def stats(tr, days):
    n = len(tr)
    if n == 0:
        return dict(n=0, win=0, avg=0, pf=0, tot=0, mdd=0, per_day=0, sl=0, cost=0)
    rs = [t["R"] for t in tr]
    w = sum(r for r in rs if r > 0)
    l = -sum(r for r in rs if r < 0)
    eq = pk = mdd = 0.0
    for t in sorted(tr, key=lambda t: t["t_exit"]):
        eq += t["R"]
        pk = max(pk, eq)
        mdd = max(mdd, pk - eq)
    return dict(
        n=n, win=100 * sum(1 for r in rs if r > 0) / n, avg=sum(rs) / n,
        pf=(w / l if l > 0 else 99.0), tot=sum(rs), mdd=mdd, per_day=n / max(days, 1),
        sl=100 * sum(t["sl_pct"] for t in tr) / n, cost=sum(t["cost_R"] for t in tr) / n,
    )


def describe(p):
    daily = p["bar_ms"] >= strategy.DAY_MS
    unit = "hari" if daily else "candle 4J"
    if p["mode"] == "cross":
        entry = f"EMA{p['ema_fast']}/{p['ema_n']} cross baru (≤{p['cross_n']} hari)"
        ema_txt = ""
    elif p["mode"] == "pullback":
        entry = f"Pullback ke EMA{p['pb_n']} (4J)"
        ema_txt = ""
    else:
        entry = f"Breakout {p['entry_n']} {unit}"
        ema_txt = f" • EMA{p['ema_n']}" if daily else ""
    ft = ""
    if p.get("btc"):
        ft += " • filter BTC>EMA200"
    if p.get("rs_top", 0) > 0:
        ft += f" • momentum top {p['rs_top'] * 100:.0f}%"
    trail_txt = f" • trailing {p['exit_n']} hari" if p["trail"] else ""
    hold_txt = f" • keluar paksa {p['max_hold'] * p['bar_ms'] // 3600000}j" if p["max_hold"] else ""
    return (f"{entry}{ema_txt}{trail_txt} • SL {p['atr_stop']:g}xATR • "
            f"short {'ya' if p['shorts'] else 'tidak'} • {tp_label(p)}{hold_txt}{ft}")


def line(tag, s):
    pf = "∞" if s["pf"] >= 99 else f"{s['pf']:.2f}"
    return (f"{tag}: {s['n']} trade • win {s['win']:.0f}% • rata-rata {s['avg']:+.2f}R • "
            f"PF {pf} • maxDD {s['mdd']:.1f}R")


# ---------------------------------------------------------------- inti
def run(candles):
    base_p = {**DEFAULT, **BASE, **ENTRIES[0]}
    warm = 205 if TF == "1D" else strategy.warmup(base_p) + 4
    inds = {s: Ind(d) for s, d in candles.items() if len(d) > warm + 60}
    if len(inds) < 5:
        lens = sorted(len(d) for d in candles.values())
        raise SystemExit(
            f"Data terlalu sedikit (butuh minimal 5 pair dengan >{warm + 60} candle). "
            f"Pair: {len(lens)}, candle terbanyak: {lens[-1] if lens else 0}. "
            "Lihat 'Pesan error pertama' di log fetch.")
    rank = {s: i for i, s in enumerate(inds)}
    t0 = min(i.d[warm]["t"] for i in inds.values())
    t1 = max(i.d[-1]["t"] for i in inds.values())
    split = t0 + (t1 - t0) * IS_FRAC
    d_is = (split - t0) / strategy.DAY_MS
    d_oos = (t1 - split) / strategy.DAY_MS

    mid = t0 + (split - t0) / 2
    ctx = strategy.build_ctx({s: i.d for s, i in inds.items()}, **strategy.ctx_args(base_p))
    for ind in inds.values():
        ind.ctx = ctx
    keys, results = list(GRID), []
    for entry, tp, vals in product(ENTRIES, TPS, product(*GRID.values())):
        p = {**DEFAULT, **BASE, **entry, **tp, **dict(zip(keys, vals))}
        raw = []
        for s, ind in inds.items():
            raw += simulate(ind, ind.arrays(p), p, s)
        acc = portfolio(raw, rank)
        results.append({
            "p": p,
            "A": stats([t for t in acc if t["t_entry"] < mid], d_is / 2),
            "B": stats([t for t in acc if mid <= t["t_entry"] < split], d_is / 2),
            "IS": stats([t for t in acc if t["t_entry"] < split], d_is),
            "OOS": stats([t for t in acc if t["t_entry"] >= split], d_oos),
            "raw_day": len(raw) / max(d_is + d_oos, 1),
        })

    key = lambda r: r["IS"]["avg"] * math.sqrt(max(r["IS"]["n"], 1))
    ranked = sorted([r for r in results if r["IS"]["n"] >= MIN_TRADES] or results,
                    key=key, reverse=True)
    # utamakan yang untung di KEDUA paruh data awal (lebih tahan banting daripada kebetulan)
    stable = [r for r in ranked if r["A"]["avg"] > 0 and r["B"]["avg"] > 0
              and r["A"]["n"] >= 8 and r["B"]["n"] >= 8]
    pool = stable or ranked
    best = pool[0]
    oos = best["OOS"]
    verified = (oos["n"] >= VERIFY["n"] and oos["avg"] >= VERIFY["avg"]
                and oos["pf"] >= VERIFY["pf"])
    A, B = best["A"], best["B"]
    stab_line = (f"Stabilitas data awal: paruh 1 {A['avg']:+.2f}R ({A['n']}) • "
                 f"paruh 2 {B['avg']:+.2f}R ({B['n']}) " + ("✅" if stable else "⚠️ tidak stabil"))

    fmt_d = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    rep = [
        f"📊 HASIL BACKTEST {'TRADING 4 JAM' if TF == '4H' else 'SWING HARIAN'} (Bitget)",
        f"{len(inds)} pair • {fmt_d(t0)} s/d {fmt_d(t1)} ({int(d_is + d_oos)} hari)",
        f"Biaya: fee {strategy.FEE*100:.2f}%/sisi, slippage {strategy.SLIP*100:.2f}%/sisi, "
        f"funding {strategy.FUND_DAY*100:.2f}%/hari (long). Maks {MAX_POS} posisi.",
        "",
        "PARAMETER TERPILIH (dipilih dari 70% data awal):",
        describe(best["p"]),
        line("In-sample ", best["IS"]),
        stab_line,
        line("Out-of-sample", oos),
        "",
        f"Sinyal/hari: mentah {best['raw_day']:.2f} • setelah batas posisi "
        f"{oos['per_day']:.2f} (OOS)",
        f"SL rata-rata {oos['sl']:.1f}% • biaya fee+slippage+funding rata-rata {oos['cost']:.2f}R/trade",
        f"Estimasi OOS (risiko 1%/trade, tanpa compounding): {oos['tot']:.1f}R ≈ {oos['tot']:.0f}% modal "
        f"≈ {oos['tot'] / d_oos * 365:.0f}%/tahun (target kamu 20-30%)",
        "",
    ]
    if verified:
        rep.append(f"✅ LOLOS uji out-of-sample (minimal {VERIFY['n']} trade, rata-rata ≥ "
                   f"+{VERIFY['avg']:.2f}R, PF ≥ {VERIFY['pf']:.2f}). Bukan jaminan. "
                   "Mulai dengan ukuran kecil.")
    elif oos["n"] < VERIFY["n"] and oos["avg"] > 0:
        rep.append(f"⚠️ DATA UJI TERLALU SEDIKIT (kurang dari {VERIFY['n']} trade). Hasilnya positif "
                   "tapi belum bisa dipercaya. Uji maju dengan ukuran sangat kecil.")
    else:
        rep.append("❌ BELUM LOLOS uji out-of-sample. Jangan live dengan uang besar. "
                   "Hasil bagus di data lama bisa kebetulan.")
    by_key = sorted(results, key=key, reverse=True)   # semua kombinasi, termasuk yang trade-nya sedikit

    def best_of(test):
        c = [r for r in by_key if test(r["p"])]
        ok = [r for r in c if r["IS"]["n"] >= 10]   # jangan pilih kombinasi yang nyaris tanpa trade
        return (ok or c or [None])[0]

    rep += ["", "PENGARUH FILTER PASAR (terbaik tiap kombinasi, IS -> OOS):"]
    for label, test in (
        ("Tanpa filter", lambda p: not p["btc"] and p["rs_top"] == 0),
        ("Filter BTC saja", lambda p: p["btc"] and p["rs_top"] == 0),
        ("Momentum saja", lambda p: not p["btc"] and p["rs_top"] > 0),
        ("BTC + momentum", lambda p: p["btc"] and p["rs_top"] > 0),
    ):
        r = best_of(test)
        if r:
            rep.append(f"• {label}: {r['IS']['avg']:+.2f}R ({r['IS']['n']}) -> "
                       f"{r['OOS']['avg']:+.2f}R ({r['OOS']['n']})")
    if len(ENTRIES) > 1:
        rep += ["", "PERBANDINGAN ENTRY (terbaik tiap jenis, IS -> OOS):"]
        for e in ENTRIES:
            r = best_of(lambda p, e=e: all(p[k] == v for k, v in e.items()))
            if r:
                rep.append(f"• {entry_label(e)}: {r['IS']['avg']:+.2f}R ({r['IS']['n']}) -> "
                           f"{r['OOS']['avg']:+.2f}R ({r['OOS']['n']})")
    rep += ["", "PERBANDINGAN TARGET PROFIT (terbaik tiap jenis, IS -> OOS):"]
    for tp in TPS:
        r = best_of(lambda p, tp=tp: p["tp_r"] == tp["tp_r"] and p["tp_pct"] == tp.get("tp_pct", 0.0))
        if r:
            rep.append(f"• {tp_label(r['p'])}: {r['IS']['avg']:+.2f}R ({r['IS']['n']}) -> "
                       f"{r['OOS']['avg']:+.2f}R ({r['OOS']['n']})")
    rep += ["", "3 KANDIDAT TERATAS (in-sample -> out-of-sample):"]
    for k, r in enumerate(pool[:3], 1):
        rep.append(f"{k}. {describe(r['p'])}")
        rep.append(f"   IS {r['IS']['avg']:+.2f}R ({r['IS']['n']}) -> OOS {r['OOS']['avg']:+.2f}R ({r['OOS']['n']})")
    rep += ["", "⚠️ Memakai pair teratas HARI INI (bias survivor: koin yang mati tidak ikut).",
            "Hasil lalu tidak menjamin masa depan. Bukan nasihat keuangan."]
    text = "\n".join(rep)

    with open("hasil_backtest.txt", "w") as f:
        f.write(text)
    with open("params.json", "w") as f:
        json.dump({
            "params": best["p"], "verified": verified, "stable": bool(stable),
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "oos": {k: round(v, 3) for k, v in oos.items()},
        }, f, indent=2)
    chunk = ""
    for block in text.split("\n\n"):
        if chunk and len(chunk) + len(block) > 3500:
            common.send(chunk, parse_mode=None)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    if chunk:
        common.send(chunk, parse_mode=None)
    return best, verified


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("fetch", "all"):
        fetch()
    if mode in ("run", "all"):
        run(load())
