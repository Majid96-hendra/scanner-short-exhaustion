import os
import requests
import ccxt
import pandas as pd
from datetime import datetime

# Token Bot Telegram dan Chat ID kamu
TELEGRAM_TOKEN = "8742883928:AAHMB4INt3kndUqGXGql6z4k7Wk5Gw_uAFc"
CHAT_ID = "8626570783"

def kirim_telegram(pesan):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": pesan,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Gagal kirim ke telegram: {e}")

def hitung_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def hitung_macd(series, fast=12, slow=26, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def scan_pasar_crypto():
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Memulai pemindaian Crypto Reversal pada {waktu_sekarang}...")
    sinyal_ditemukan = []

    try:
        exchange = ccxt.binance({'options': {'defaultType': 'future'}, 'enableRateLimit': True})
        markets = exchange.load_markets()
        symbols = [s for s in markets if s.endswith('/USDT') and not ':USDT' in s and markets[s]['active']]
        
        for symbol in symbols:
            try:
                # Mengambil data kline/OHLCV timeframe 15m
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                if len(ohlcv) < 50: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # 1. Hitung Indikator (RSI & MACD)
                df['rsi'] = hitung_rsi(df['close'], 14)
                df['macd'], df['signal'] = hitung_macd(df['close'])
                
                if len(df) < 3: continue
                
                rsi_sebelumnya = df['rsi'].iloc[-2]
                rsi_sekarang = df['rsi'].iloc[-1]
                
                macd_sebelumnya = df['macd'].iloc[-2]
                signal_sebelumnya = df['signal'].iloc[-2]
                macd_sekarang = df['macd'].iloc[-1]
                signal_sekarang = df['signal'].iloc[-1]
                
                harga_sekarang = df['close'].iloc[-1]
                open_terakhir = df['open'].iloc[-1]
                high_terakhir = df['high'].iloc[-1]
                
                vol_rata2 = df['volume'].iloc[:-1].mean()
                vol_terakhir = df['volume'].iloc[-1]
                
                # --- VALIDASI KRITERIA REVERSAL SHORT ---
                # A. Kondisi RSI: Sempat di atas 68/70 lalu turun (Overbought Reversal)
                kondisi_rsi = (rsi_sebelumnya >= 68) and (rsi_sekarang < rsi_sebelumnya)
                
                # B. Kondisi MACD: Terjadi Crossover Negatif (MACD memotong ke bawah garis signal di atas 0)
                kondisi_macd = (macd_sebelumnya >= signal_sebelumnya) and (macd_sekarang < signal_sekarang) and (macd_sekarang > 0)
                
                # C. Kondisi Volume & Harga: Volume lonjakan penjualan / Rejection (Close < Open atau Ekor Atas)
                kondisi_rejection = (harga_sekarang < open_terakhir) or (vol_terakhir > vol_rata2 * 1.3)
                
                # Jika minimal RSI Reversal terpenuhi didukung penolakan harga atau MACD negatif
                if kondisi_rsi and (kondisi_rejection or kondisi_macd):
                    entry = harga_sekarang
                    sl = high_terakhir * 1.008
                    risiko = sl - entry
                    if risiko <= 0: continue
                    tp1 = entry - (risiko * 1.5)
                    tp2 = entry - (risiko * 2.5)
                    
                    teks = (f"🚨 *SINYAL SHORT CRYPTO REVERSAL* 🚨\n"
                            f"• Aset: `{symbol}`\n"
                            f"• RSI: `{rsi_sebelumnya:.1f}` ➔ `{rsi_sekarang:.1f}`\n"
                            f"• MACD Cross: `{'Ya 🔻' if kondisi_macd else 'Tidak'}`\n"
                            f"• Entry: `{entry:.4f}`\n"
                            f"• Stop Loss: `{sl:.4f}`\n"
                            f"• TP1: `{tp1:.4f}` | TP2: `{tp2:.4f}`")
                    sinyal_ditemukan.append(teks)
            except:
                continue
    except Exception as e:
        print(f"Error crypto: {e}")

    # Laporan ke Telegram
    if len(sinyal_ditemukan) > 0:
        kirim_telegram(f"🔍 *Hasil Scan Crypto Reversal ({waktu_sekarang}) - Ditemukan {len(sinyal_ditemukan)} Sinyal:*")
        for item in sinyal_ditemukan:
            kirim_telegram(item)
    else:
        kirim_telegram(f"🤖 *Scan Update ({waktu_sekarang}):* Belum ada koin crypto yang memenuhi kriteria pembalikan Short.")

if __name__ == "__main__":
    scan_pasar_crypto()
