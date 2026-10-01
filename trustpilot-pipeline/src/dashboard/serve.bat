@echo off
REM Trustpilot dashboard — local server with auto-open
REM Double-click this file. Server starts and your browser opens to the dashboard.

cd /d "%~dp0"
echo.
echo Trustpilot dashboard serving at http://localhost:8080
echo Opening your browser...
echo Press Ctrl+C to stop the server.
echo.

REM Open browser after a short delay so the server has time to bind
start "" cmd /c "timeout /t 1 /nobreak >nul && start http://localhost:8080"

REM Try python first, then py launcher
python -m http.server 8080 2>nul
if errorlevel 1 py -m http.server 8080
pause
