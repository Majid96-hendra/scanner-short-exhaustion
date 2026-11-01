#!/usr/bin/env python3
"""
Scanner SWING harian (Bitget USDT-M Futures) -> Telegram.
Strategi dan parameter sama dengan backtest (params.json hasil backtest dipakai otomatis).
Jalankan sekali sehari, setelah candle harian close.
"""
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import common
import strategy

TOP_N = int(os.getenv("TOP_N", "100"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))
MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "3"))
DAILY_REPORT = os.getenv("DAILY_REPORT", "1") == "1"  # laporan harian saat tidak ada sinyal
WIB = timezone(timedelta(hours=7))


def load_params():
    try:
        with open("params.json") as f:
            j = json.load(f)
        return {**strategy.DEFAULT, **j["params"]}, bool(j.get("verified", False))
    except (OSError, ValueError, KeyError):
        return dict(strategy.DEFAULT), False


def fmt(x):
    if x >= 1000:
        return f"{x:.1f}"
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.3f}"
    if x >= 0.1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def enrich(sig):
    """Order book + funding hanya KONFIRMASI (mengubah skor), bukan pemicu sinyal."""
    side = sig["side"]
    score = 5
    if sig["vol_ratio"] >= 1.5:
        score += 1
    if 5 <= abs(sig["dist200"]) <= 60:
        score += 1
    if abs(sig["chg"]) >= 20:
        score += 1
    try:
        r = common.book_ratio(sig["symbol"])
        conf = r if side == "LONG" else 1 / r
    except (RuntimeError, KeyError, ValueError, TypeError, ZeroDivisionError):
        conf = None
    sig["book"] = conf
    if conf is not None:
        if conf >= 1.15:
            score += 2
        elif conf < 0.87:
            score -= 1
    f = common.funding(sig["symbol"])
    sig["funding"] = f
    if f is not None and ((side == "LONG" and f > 0.05) or (side == "SHORT" and f < -0.05)):
        score -= 1
    sig["score"] = max(0, min(10, score))
    return sig


def message(s, p, verified):
    long_ = s["side"] == "LONG"
    entry = s["entry"]
    stars = "⭐" * round(s["score"] / 2)
    if p["mode"] == "cross":
        entry_txt = (f"EMA{p['ema_fast']}/{p['ema_n']} cross "
                     f"{'naik' if long_ else 'turun'} baru")
    else:
        entry_txt = f"{'Breakout' if long_ else 'Breakdown'} {p['entry_n']} hari"
    t_close = datetime.fromtimestamp(s["t_close"] / 1000, WIB).strftime("%d-%m %H:%M WIB")
    lines = [
        f"{'🟢' if long_ else '🔴'} <b>{s['side']} • {s['symbol']}</b>",
        "━━━━━━━━━━━━━━",
        f"⏱ Swing harian • {entry_txt}",
        f"📈 Tren {p['trend_lb']} hari: {'Naik' if long_ else 'Turun'} ({s['chg']:+.0f}%) ✅",
        f"{stars} Skor: <b>{s['score']}/10</b>",
        "",
        f"📍 <b>Entry</b>    : <code>{fmt(entry)}</code> (market/limit dekat harga ini)",
        f"🛑 <b>SL awal</b>  : <code>{fmt(s['sl'])}</code> (-{s['sl_pct']:.1f}%)",
    ]
    if s["tp1"]:
        pct = abs(s["tp1"] / entry - 1) * 100
        if s.get("tp_full"):
            lines.append(f"🎯 <b>TP</b>       : <code>{fmt(s['tp1'])}</code> "
                         f"(+{pct:.0f}%) → tutup semua")
        else:
            lines.append(f"🎯 <b>TP1</b>      : <code>{fmt(s['tp1'])}</code> "
                         f"({int(p['tp_frac'] * 100)}%) +{pct:.1f}% → lalu SL ke entry")
    tail = "Sebelum TP kena, SL tetap boleh naik." if s.get("tp_full") else \
        "Tanpa TP tetap: biarkan tren berjalan."
    lines.append(f"🔔 <b>Trailing</b> : setelah candle harian close, SL ikuti "
                 f"{'low' if long_ else 'high'} {p['exit_n']} hari bila lebih ketat "
                 f"(kini <code>{fmt(s['trail'])}</code>). {tail}")
    lines += ["", f"⚖️ Risiko maks 1% modal • ukuran = 1% ÷ {s['sl_pct']:.1f}%",
              f"💸 Biaya fee+slippage ≈ {s['cost_r']:.2f}R"]
    b = s["book"]
    if b is None:
        lines.append("📊 Order book: tidak tersedia")
    elif b >= 1.15:
        lines.append(f"📊 Order book: searah ✅ ({b:.2f}x)")
    elif b >= 0.87:
        lines.append(f"📊 Order book: netral ➖ ({b:.2f}x)")
    else:
        lines.append(f"📊 Order book: melawan ⚠️ ({b:.2f}x)")
    if s["funding"] is not None:
        lines.append(f"💰 Funding: {s['funding']:+.3f}%")
    if not long_:
        lines.append("⚠️ Short rawan squeeze, disiplin di SL")
    if not verified:
        lines.append("🧪 Parameter belum lolos uji out-of-sample: ukuran kecil dulu")
    lines += ["━━━━━━━━━━━━━━", f"🕐 Candle close: {t_close}",
              "<i>Bukan nasihat keuangan.</i>"]
    return "\n".join(lines)


def report(n_pairs, stats, found, p):
    ups = [s for s in stats if s["up"]]
    cross = p["mode"] == "cross"
    if cross:
        near = sorted((s for s in stats if 0 < s["gap"] <= 3 and s["approach"]),
                      key=lambda s: s["gap"])[:3]
    else:
        near = sorted((s for s in ups if 0 < s["to_high"] <= 5), key=lambda s: s["to_high"])[:3]
    t_close = datetime.fromtimestamp(max(s["t_close"] for s in stats) / 1000, WIB)
    lines = ["😴 <b>Laporan harian swing</b>: tidak ada sinyal", "━━━━━━━━━━━━━━",
             f"Pair dipindai: {len(stats)} dari {n_pairs}",
             f"Lolos filter tren naik: {len(ups)}"]
    if p["shorts"]:
        lines.append(f"Lolos filter tren turun: {sum(1 for s in stats if s['dn'])}")
    if found:
        lines.append(f"Sinyal muncul tapi skor di bawah batas: {len(found)}")
    if near:
        if cross:
            lines.append(f"Hampir cross (EMA{p['ema_fast']} tinggal sedikit di bawah EMA{p['ema_n']}):")
            lines += [f"• {s['symbol']}  celah {s['gap']:.1f}%" for s in near]
        else:
            lines.append(f"Hampir breakout (jarak ke high {p['entry_n']} hari):")
            lines += [f"• {s['symbol']}  +{s['to_high']:.1f}%" for s in near]
    else:
        lines.append("Belum ada yang dekat cross." if cross else "Belum ada yang dekat breakout.")
    if len(ups) <= 2:
        lines.append("Pasar belum searah tren naik, wajar sepi. Jangan paksa entry.")
    lines += ["━━━━━━━━━━━━━━", f"🕐 Data s/d candle close {t_close:%d-%m %H:%M} WIB",
              "<i>Bot hidup ✅</i>"]
    return "\n".join(lines)


def main():
    p, verified = load_params()
    try:
        symbols = common.universe(TOP_N)
    except RuntimeError as e:
        common.send(f"⚠️ Scanner gagal: {e}")
        sys.exit(1)

    need = strategy.warmup(p) + 5
    found, stats, lens, errors, first_err = [], [], [], 0, ""
    for sym in symbols:
        try:
            d, herr = common.candles_history(sym, "1D", need + 30)
            lens.append(len(d))
            if not d and herr:
                raise RuntimeError(herr)
            sig = strategy.live_signal(d, p)
            if sig:
                sig["symbol"] = sym
                found.append(sig)
            st = strategy.status(d, p)
            if st:
                st["symbol"] = sym
                stats.append(st)
        except RuntimeError as e:
            errors += 1
            first_err = first_err or str(e)
        time.sleep(0.12)

    scored = [enrich(s) for s in found]
    good = sorted((s for s in scored if s["score"] >= MIN_SCORE),
                  key=lambda s: s["score"], reverse=True)[:MAX_SIGNALS]
    print(f"Scan {len(symbols)} pair | sinyal {len(found)} | lolos skor {len(good)} | error {errors}")

    for s in good:
        common.send(message(s, p, verified))
    if not good and DAILY_REPORT:
        if stats:
            common.send(report(len(symbols), stats, found, p))
        else:
            short = sum(1 for n in lens if n < need)
            common.send(
                "⚠️ Scanner jalan tapi data tidak bisa dipakai.\n"
                f"Gagal diambil: {errors} pair • riwayat terlalu pendek: {short} pair\n"
                f"Riwayat terpanjang: {max(lens) if lens else 0} hari (butuh {need})\n"
                f"Error pertama: {html.escape(first_err) or '-'}")


if __name__ == "__main__":
    main()
