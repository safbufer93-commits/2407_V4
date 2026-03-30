@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_one_click.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Launcher failed with exit code %EXIT_CODE%.
  echo Press any key to close this window...
  pause >nul
)

endlocal & exit /b %EXIT_CODE%
