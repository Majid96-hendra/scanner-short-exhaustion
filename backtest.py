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

DATA_FILE = os.getenv("DATA_FILE", "data.json")
TOP_N = int(os.getenv("TOP_N", "60"))
MAX_POS = int(os.getenv("MAX_POS", "2"))
IS_FRAC = 0.7
MIN_TRADES = 30
GRID = {
    "entry_n": [20, 30, 55],
    "exit_n": [10, 20],
    "ema_n": [100, 200],
    "atr_stop": [2.0, 3.0],
    "shorts": [True, False],
    "tp_r": [0.0, 3.0],
}


# ---------------------------------------------------------------- data
def fetch():
    syms = common.universe(TOP_N)
    data = {}
    for s in syms:
        try:
            cs = common.candles(s, "1D", 1000)
            data[s] = [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in cs]
        except RuntimeError as e:
            print("lewati", s, e)
        time.sleep(0.12)
    with open(DATA_FILE, "w") as f:
        json.dump({"symbols": list(data), "candles": data}, f)
    print(f"Data tersimpan: {len(data)} pair")


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
    tp = f"TP parsial {p['tp_r']:g}R (50%)" if p["tp_r"] > 0 else "tanpa TP tetap (trailing)"
    return (f"Breakout {p['entry_n']} hari • trailing {p['exit_n']} hari • EMA{p['ema_n']} • "
            f"SL {p['atr_stop']:g}xATR • short {'ya' if p['shorts'] else 'tidak'} • {tp}")


def line(tag, s):
    pf = "∞" if s["pf"] >= 99 else f"{s['pf']:.2f}"
    return (f"{tag}: {s['n']} trade • win {s['win']:.0f}% • rata-rata {s['avg']:+.2f}R • "
            f"PF {pf} • maxDD {s['mdd']:.1f}R")


# ---------------------------------------------------------------- inti
def run(candles):
    inds = {s: Ind(d) for s, d in candles.items() if len(d) > 260}
    if len(inds) < 5:
        raise SystemExit("Data terlalu sedikit (butuh minimal 5 pair dengan >260 hari).")
    rank = {s: i for i, s in enumerate(inds)}
    t0 = min(i.d[205]["t"] for i in inds.values())
    t1 = max(i.d[-1]["t"] for i in inds.values())
    split = t0 + (t1 - t0) * IS_FRAC
    d_is = (split - t0) / strategy.DAY_MS
    d_oos = (t1 - split) / strategy.DAY_MS

    keys, results = list(GRID), []
    for vals in product(*GRID.values()):
        p = {**DEFAULT, **dict(zip(keys, vals))}
        raw = []
        for s, ind in inds.items():
            raw += simulate(ind, ind.arrays(p), p, s)
        acc = portfolio(raw, rank)
        results.append({
            "p": p,
            "IS": stats([t for t in acc if t["t_entry"] < split], d_is),
            "OOS": stats([t for t in acc if t["t_entry"] >= split], d_oos),
            "raw_day": len(raw) / max(d_is + d_oos, 1),
        })

    pool = [r for r in results if r["IS"]["n"] >= MIN_TRADES] or results
    pool.sort(key=lambda r: r["IS"]["avg"] * math.sqrt(max(r["IS"]["n"], 1)), reverse=True)
    best = pool[0]
    oos = best["OOS"]
    verified = oos["n"] >= 15 and oos["avg"] >= 0.1 and oos["pf"] >= 1.2

    fmt_d = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    rep = [
        "📊 HASIL BACKTEST SWING HARIAN (Bitget)",
        f"{len(inds)} pair • {fmt_d(t0)} s/d {fmt_d(t1)} ({int(d_is + d_oos)} hari)",
        f"Biaya: fee {strategy.FEE*100:.2f}%/sisi, slippage {strategy.SLIP*100:.2f}%/sisi, "
        f"funding {strategy.FUND_DAY*100:.2f}%/hari (long). Maks {MAX_POS} posisi.",
        "",
        "PARAMETER TERPILIH (dipilih dari 70% data awal):",
        describe(best["p"]),
        line("In-sample ", best["IS"]),
        line("Out-of-sample", oos),
        "",
        f"Sinyal/hari: mentah {best['raw_day']:.2f} • setelah batas posisi "
        f"{oos['per_day']:.2f} (OOS)",
        f"SL rata-rata {oos['sl']:.1f}% • biaya fee+slippage+funding rata-rata {oos['cost']:.2f}R/trade",
        f"Estimasi OOS (risiko 1%/trade, tanpa compounding): {oos['tot']:.1f}R ≈ {oos['tot']:.0f}% modal",
        "",
    ]
    if verified:
        rep.append("✅ LOLOS uji out-of-sample (OOS rata-rata ≥ +0,1R dan PF ≥ 1,2). "
                   "Bukan jaminan. Mulai dengan ukuran kecil.")
    else:
        rep.append("❌ BELUM LOLOS uji out-of-sample. Jangan live dengan uang besar. "
                   "Hasil bagus di data lama bisa kebetulan.")
    rep += ["", "5 KANDIDAT TERATAS (in-sample -> out-of-sample):"]
    for k, r in enumerate(pool[:5], 1):
        rep.append(f"{k}. {describe(r['p'])}")
        rep.append(f"   IS {r['IS']['avg']:+.2f}R ({r['IS']['n']}) -> OOS {r['OOS']['avg']:+.2f}R ({r['OOS']['n']})")
    rep += ["", "⚠️ Memakai pair teratas HARI INI (bias survivor: koin yang mati tidak ikut).",
            "Hasil lalu tidak menjamin masa depan. Bukan nasihat keuangan."]
    text = "\n".join(rep)

    with open("hasil_backtest.txt", "w") as f:
        f.write(text)
    with open("params.json", "w") as f:
        json.dump({
            "params": best["p"], "verified": verified,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "oos": {k: round(v, 3) for k, v in oos.items()},
        }, f, indent=2)
    common.send(text, parse_mode=None)
    return best, verified


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("fetch", "all"):
        fetch()
    if mode in ("run", "all"):
        run(load())
