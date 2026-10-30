# Swing Scanner (Bitget Futures -> Telegram)

Strategi swing harian ikut tren. Parameter dicari sendiri oleh backtest (dengan fee,
slippage, funding) dan diuji di data yang tidak dipakai untuk memilih (out-of-sample).

## File
- scanner.py   : scan harian, kirim sinyal ke Telegram
- backtest.py  : ambil data, cari parameter, uji, kirim ringkasan ke Telegram, tulis params.json
- strategy.py, common.py : logika strategi dan API Bitget
- workflow-file/scan.yml, backtest.yml : salin ke .github/workflows/

## Setup
1. Telegram: buat bot di @BotFather (dapat TOKEN), kirim satu pesan ke bot, lalu buka
   https://api.telegram.org/bot<TOKEN>/getUpdates untuk melihat chat id.
2. Upload semua file .py dan requirements.txt ke root repo GitHub.
3. Salin kedua file di workflow-file/ ke folder .github/workflows/.
4. Settings > Secrets and variables > Actions: tambah TELEGRAM_TOKEN dan TELEGRAM_CHAT_ID.
5. Tab Actions > "Backtest Swing" > Run workflow. Hasil masuk ke Telegram dan params.json
   tersimpan otomatis. LIHAT VERDICT-nya.
6. Hanya jika lolos uji, biarkan "Swing Scanner" jalan tiap hari (atau uji manual dulu).

## Catatan
- Jika Bitget memblokir IP GitHub (error 403/451), jalankan di VPS dengan cron.
- Ulangi backtest tiap 1-2 bulan; jangan ubah-ubah parameter sendiri tiap kalah.
