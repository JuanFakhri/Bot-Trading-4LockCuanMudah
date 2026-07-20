@echo off
REM NestSMC - peluncur non-interaktif untuk Task Scheduler / auto-start.
REM Prasyarat: sudah pernah jalankan start-bot.bat sekali (agar nestsmc.env terisi).
cd /d "%~dp0\..\.."
set "CFG=%~dp0nestsmc.env"
if exist "%CFG%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
)
python -m scripts.run_bot >> "%~dp0nestsmc.log" 2>&1
