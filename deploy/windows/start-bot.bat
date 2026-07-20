@echo off
REM ============================================================================
REM  NestSMC — peluncur bot Windows (sekali-klik).
REM  Taruh file ini di: <repo>\deploy\windows\  lalu DOBEL-KLIK.
REM  Mode NOTIFIKASI: bot kirim sinyal ke Telegram, TIDAK membuka order.
REM ============================================================================
setlocal enabledelayedexpansion
title NestSMC Bot
cd /d "%~dp0\..\.."
echo ==============================================
echo   NestSMC - Bot Trading (Windows) - NOTIFIKASI
echo ==============================================
echo.

REM --- 1) cek Python ---------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python belum terpasang.
  echo     Install dari https://www.python.org/downloads/
  echo     dan CENTANG "Add Python to PATH" saat memasang.
  echo.
  pause
  exit /b 1
)

REM --- 2) pasang library (sekali; cepat jika sudah ada) ----------------------
echo [*] Memeriksa/memasang library (numpy, pandas, httpx)...
python -m pip install --quiet --upgrade numpy pandas httpx
if errorlevel 1 (
  echo [X] Gagal memasang library. Cek koneksi internet lalu ulangi.
  pause
  exit /b 1
)

REM --- 3) muat konfigurasi tersimpan (token + chat id) -----------------------
set "CFG=%~dp0nestsmc.env"
if exist "%CFG%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
)

REM --- 4) token --------------------------------------------------------------
if "%TELEGRAM_TOKEN%"=="" (
  echo.
  echo Buka @BotFather - kirim /token - pilih bot-mu untuk dapat TOKEN.
  set /p "TELEGRAM_TOKEN=Tempel TOKEN bot Telegram di sini: "
)

REM --- 5) chat_id (otomatis) --------------------------------------------------
if "%TELEGRAM_CHAT_ID%"=="" (
  echo.
  echo Di Telegram: buka @SimpleCuan_bot lalu tekan START / kirim "halo".
  echo Kalau sudah, tekan tombol apa saja di sini...
  pause >nul
  echo [*] Mengambil chat_id otomatis...
  for /f "usebackq delims=" %%i in (`python -m scripts.telegram_chatid`) do set "TELEGRAM_CHAT_ID=%%i"
)
if "%TELEGRAM_CHAT_ID%"=="" (
  echo [!] chat_id belum ketemu. Pastikan sudah tekan START di bot, lalu isi manual.
  set /p "TELEGRAM_CHAT_ID=Masukkan chat_id (angka): "
)

REM --- 6) simpan konfigurasi biar tak usah ketik ulang -----------------------
> "%CFG%" echo # NestSMC - jangan bagikan file ini (berisi token)
>>"%CFG%" echo TELEGRAM_TOKEN=%TELEGRAM_TOKEN%
>>"%CFG%" echo TELEGRAM_CHAT_ID=%TELEGRAM_CHAT_ID%

REM --- 7) jalankan bot -------------------------------------------------------
echo.
echo [*] Bot jalan. Cek Telegram untuk pesan "NestSMC aktif".
echo     Biarkan jendela ini TERBUKA. Tutup jendela = bot berhenti.
echo.
python -m scripts.run_bot

echo.
echo [i] Bot berhenti. Tekan tombol apa saja untuk menutup.
pause >nul
