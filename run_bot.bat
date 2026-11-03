@echo off
chcp 65001 >nul
rem ربات را روشن نگه می‌دارد: اگر کرش کرد یا اینترنت قطع شد، خودش دوباره روشن می‌شود.
rem برای اجرای خودکار بعد از هر بار روشن شدن ویندوز، فایل install_startup.bat را یک بار اجرا کن.
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

:loop
echo [%date% %time%] starting bot...
"%PY%" rubika_bot.py >> rubika_bot.log 2>&1
echo [%date% %time%] bot stopped (crash or network); restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
