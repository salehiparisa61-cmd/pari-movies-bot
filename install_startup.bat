@echo off
chcp 65001 >nul
rem یک بار اجرا کن تا ربات موقع روشن شدن ویندوز خودکار بالا بیاید.
cd /d "%~dp0"

schtasks /Create /TN "PariMoviesBot" /SC ONLOGON /TR "\"%~dp0run_bot.bat\"" /F
if errorlevel 1 (
  echo.
  echo ساخت تسک خودکار ناموفق بود. راه دستی:
  echo   1^) کلید Win+R و تایپ: shell:startup
  echo   2^) میان‌بر ^(shortcut^) فایل run_bot.bat را داخل پوشه‌ای که باز شد کپی کن
) else (
  echo.
  echo انجام شد. از این به بعد با هر بار روشن شدن ویندوز، ربات خودکار اجرا می‌شود.
  echo برای حذف:  schtasks /Delete /TN "PariMoviesBot" /F
)
echo.
pause
