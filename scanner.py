import os
import requests
import ccxt
import yfinance as yf
import pandas as pd
from datetime import datetime

# Token Bot Telegram dan Chat ID kamu (sudah otomatis terisi)
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

def scan_pasar():
    print("Memulai pemindaian Short Exhaustion (Crypto & Saham)...")
    sinyal_ditemukan = []

    # 1. Scan Crypto Futures (Binance 15m)
    try:
        exchange = ccxt.binance({'options': {'defaultType': 'future'}, 'enableRateLimit': True})
        markets = exchange.load_markets()
        symbols = [s for s in markets if s.endswith('/USDT') and not ':USDT' in s and markets[s]['active']]
        
        for symbol in symbols[:50]: # Batasi 50 koin teratas agar proses cepat
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
                if len(ohlcv) < 20: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                harga_sekarang = df['close'].iloc[-1]
                open_terakhir = df['open'].iloc[-1]
                high_terakhir = df['high'].iloc[-1]
                kenaikan = ((df['close'].iloc[-2] - df['close'].iloc[0]) / df['close'].iloc[0]) * 100
                vol_rata2 = df['volume'].iloc[:-1].mean()
                vol_terakhir = df['volume'].iloc[-1]
                
                # Logika Short Exhaustion
                if vol_terakhir > (vol_rata2 * 1.5) and kenaikan > 2.5 and harga_sekarang < open_terakhir:
                    entry = harga_sekarang
                    sl = high_terakhir * 1.008
                    risiko = sl - entry
                    if risiko <= 0: continue
                    tp1 = entry - (risiko * 1.5)
                    tp2 = entry - (risiko * 2.5)
                    
                    teks = (f"🚨 *SINYAL SHORT CRYPTO* 🚨\n"
                            f"• Aset: `{symbol}`\n"
                            f"• Entry: `{entry:.4f}`\n"
                            f"• Stop Loss: `{sl:.4f}`\n"
                            f"• TP1: `{tp1:.4f}` | TP2: `{tp2:.4f}`")
                    sinyal_ditemukan.append(teks)
            except:
                continue
    except Exception as e:
        print(f"Error crypto: {e}")

    # 2. Scan Saham (Contoh: US Stocks)
    daftar_saham = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN']
    for ticker in daftar_saham:
        try:
            df = yf.download(ticker, period='5d', interval='1h', progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) < 20: continue
            
            harga_sekarang = df['Close'].iloc[-1]
            open_terakhir = df['Open'].iloc[-1]
            high_terakhir = df['High'].iloc[-1]
            kenaikan = ((df['Close'].iloc[-2] - df['Close'].iloc[0]) / df['Close'].iloc[0]) * 100
            vol_rata2 = df['Volume'].iloc[:-1].mean()
            vol_terakhir = df['Volume'].iloc[-1]
            
            if vol_terakhir > (vol_rata2 * 1.3) and kenaikan > 2.0 and harga_sekarang < open_terakhir:
                entry = harga_sekarang
                sl = high_terakhir * 1.008
                risiko = sl - entry
                if risiko <= 0: continue
                tp1 = entry - (risiko * 1.5)
                
                teks = (f"📈 *SINYAL SHORT SAHAM* 📈\n"
                        f"• Ticker: `{ticker}`\n"
                        f"• Entry: `{entry:.2f}`\n"
                        f"• Stop Loss: `{sl:.2f}`\n"
                        f"• TP1: `{tp1:.2f}`")
                sinyal_ditemukan.append(teks)
        except:
            continue

    # Kirim hasil ke Telegram
    if len(sinyal_ditemukan) > 0:
        kirim_telegram(f"🔍 *Hasil Scan Ditemukan ({len(sinyal_ditemukan)} Sinyal):*")
        for item in sinyal_ditemukan:
            kirim_telegram(item)
    else:
        kirim_telegram("🤖 *Scan Selesai:* Belum ada aset yang memenuhi kriteria Short Exhaustion saat ini.")

if __name__ == "__main__":
    scan_pasar()
