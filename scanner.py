import os
import requests
import ccxt
import yfinance as yf
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

def scan_pasar():
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Memulai pemindaian optimal pada {waktu_sekarang}...")
    sinyal_ditemukan = []

    # 1. Scan SEMUA Koin Crypto Futures di Binance (Timeframe 15m)
    try:
        exchange = ccxt.binance({'options': {'defaultType': 'future'}, 'enableRateLimit': True})
        markets = exchange.load_markets()
        # Mengambil semua koin /USDT tanpa dibatasi agar tidak ada yang lolos
        symbols = [s for s in markets if s.endswith('/USDT') and not ':USDT' in s and markets[s]['active']]
        
        for symbol in symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
                if len(ohlcv) < 30: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # Hitung RSI 14
                df['rsi'] = hitung_rsi(df['close'], 14)
                if len(df['rsi']) < 3: continue
                
                rsi_sebelumnya = df['rsi'].iloc[-2]
                rsi_sekarang = df['rsi'].iloc[-1]
                harga_sekarang = df['close'].iloc[-1]
                open_terakhir = df['open'].iloc[-1]
                high_terakhir = df['high'].iloc[-1]
                
                vol_rata2 = df['volume'].iloc[:-1].mean()
                vol_terakhir = df['volume'].iloc[-1]
                
                # KRITERIA DIOPTIMALKAN:
                # 1. RSI sempat overbought (>= 68 atau 70) lalu berbalik turun
                # 2. Ada lonjakan volume (vol_terakhir > vol_rata2 * 1.2) atau harga mulai rejection (Close < Open)
                kondisi_rsi = (rsi_sebelumnya >= 68) and (rsi_sekarang < rsi_sebelumnya)
                kondisi_rejection = (harga_sekarang < open_terakhir) or (vol_terakhir > vol_rata2 * 1.3)
                
                if kondisi_rsi and kondisi_rejection:
                    entry = harga_sekarang
                    sl = high_terakhir * 1.008
                    risiko = sl - entry
                    if risiko <= 0: continue
                    tp1 = entry - (risiko * 1.5)
                    tp2 = entry - (risiko * 2.5)
                    
                    teks = (f"🔥 *POTENSI SHORT CRYPTO* 🔥\n"
                            f"• Aset: `{symbol}`\n"
                            f"• RSI: `{rsi_sebelumnya:.1f}` ➔ `{rsi_sekarang:.1f}`\n"
                            f"• Entry: `{entry:.4f}`\n"
                            f"• Stop Loss: `{sl:.4f}`\n"
                            f"• TP1: `{tp1:.4f}` | TP2: `{tp2:.4f}`")
                    sinyal_ditemukan.append(teks)
            except:
                continue
    except Exception as e:
        print(f"Error crypto: {e}")

    # 2. Scan Saham (US Stocks)
    daftar_saham = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN', 'META', 'GOOGL']
    for ticker in daftar_saham:
        try:
            df = yf.download(ticker, period='5d', interval='1h', progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if len(df) < 30: continue
            
            df['rsi'] = hitung_rsi(df['Close'], 14)
            rsi_sebelumnya = df['rsi'].iloc[-2]
            rsi_sekarang = df['rsi'].iloc[-1]
            harga_sekarang = df['Close'].iloc[-1]
            high_terakhir = df['High'].iloc[-1]
            
            if rsi_sebelumnya >= 68 and rsi_sekarang < rsi_sebelumnya:
                entry = harga_sekarang
                sl = high_terakhir * 1.008
                risiko = sl - entry
                if risiko <= 0: continue
                tp1 = entry - (risiko * 1.5)
                
                teks = (f"📉 *POTENSI SHORT SAHAM* 📉\n"
                        f"• Ticker: `{ticker}`\n"
                        f"• RSI: `{rsi_sebelumnya:.1f}` ➔ `{rsi_sekarang:.1f}`\n"
                        f"• Entry: `{entry:.2f}`\n"
                        f"• Stop Loss: `{sl:.2f}`\n"
                        f"• TP1: `{tp1:.2f}`")
                sinyal_ditemukan.append(teks)
        except:
            continue

    # Laporan ke Telegram
    if len(sinyal_ditemukan) > 0:
        kirim_telegram(f"🔍 *Hasil Scan Optimal ({waktu_sekarang}) - Ditemukan {len(sinyal_ditemukan)} Sinyal:*")
        for item in sinyal_ditemukan:
            kirim_telegram(item)
    else:
        kirim_telegram(f"🤖 *Scan Update ({waktu_sekarang}):* Belum ada koin/saham yang masuk kriteria reversal RSI.")

if __name__ == "__main__":
    scan_pasar()
