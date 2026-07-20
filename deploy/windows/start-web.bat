@echo off
REM ============================================================================
REM  NestSMC - DASHBOARD REALTIME (aplikasi web penuh) untuk VPS/PC Windows.
REM  Scan 60 detik + Telegram + dashboard realtime (WebSocket) di port 8000.
REM  JANGAN jalankan bersamaan dengan start-bot.bat (cukup salah satu).
REM ============================================================================
setlocal enabledelayedexpansion
title NestSMC Web (realtime)
cd /d "%~dp0\..\.."

where python >nul 2>nul || (echo [X] Python belum terpasang. & pause & exit /b 1)

echo [*] Memasang library web (fastapi, uvicorn, numpy, pandas, httpx)...
python -m pip install --quiet --upgrade numpy pandas httpx fastapi "uvicorn[standard]"
if errorlevel 1 (echo [X] Gagal pasang library. & pause & exit /b 1)

REM muat token+chat id (dari start-bot.bat) bila ada
set "CFG=%~dp0nestsmc.env"
if exist "%CFG%" for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
if "%TELEGRAM_TOKEN%"=="" set /p "TELEGRAM_TOKEN=Tempel TOKEN bot Telegram (Enter untuk lewati): "
if not "%TELEGRAM_TOKEN%"=="" if "%TELEGRAM_CHAT_ID%"=="" set /p "TELEGRAM_CHAT_ID=Masukkan chat_id (Enter untuk lewati): "

echo.
echo [*] Dashboard realtime jalan di:  http://localhost:8000
echo     Dari HP/luar:  http://IP-PUBLIK-VPS:8000  (buka firewall port 8000 dulu)
echo     Biarkan jendela ini TERBUKA. Ctrl-C untuk berhenti.
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
pause
