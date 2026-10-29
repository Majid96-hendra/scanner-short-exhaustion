# Trend Scanner (Bitget Futures -> Telegram)

## Setup
1. Telegram: buat bot di @BotFather (dapat TOKEN). Kirim pesan ke bot, lalu buka
   https://api.telegram.org/bot<TOKEN>/getUpdates untuk melihat chat id.
2. GitHub: buat repo, upload `scanner.py` dan `requirements.txt` ke root repo.
3. Buat file `.github/workflows/scan.yml` dengan isi dari `workflow-file/scan.yml`.
4. Settings > Secrets and variables > Actions: tambah `TELEGRAM_TOKEN` dan `TELEGRAM_CHAT_ID`.
5. Tab Actions > Trend Scanner > Run workflow untuk uji manual.

## Uji lokal tanpa Telegram
DRY_RUN=1 python scanner.py

## Pengaturan (environment variable)
TOP_N=60, MIN_SCORE=6, MAX_SIGNALS=2, ATR_SL_MULT=1.5, MAX_SL_PCT=8, SEND_EMPTY=0

## Catatan
- Jika Bitget memblokir IP server GitHub (error 403/451), jalankan
  di VPS (mis. Singapura) dengan cron: `5 */4 * * * python3 /path/scanner.py`.
- Bot tidak menyimpan state, jadi jangan dijalankan lebih sering dari tiap candle 4H.
