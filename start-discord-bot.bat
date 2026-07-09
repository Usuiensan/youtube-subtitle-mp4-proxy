@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [youtube-mp4-proxy] Creating virtual environment...
  py -3.12 -m venv .venv
  if errorlevel 1 py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 goto error
)

set "PATH=%CD%\.venv\Scripts;%PATH%"

echo [youtube-mp4-proxy] Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -U pip
if errorlevel 1 goto error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo [youtube-mp4-proxy] Starting Discord bot...
echo [youtube-mp4-proxy] Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m bot.main
goto end

:error
echo.
echo [youtube-mp4-proxy] Failed to start Discord bot. See the error above.
pause

:end
endlocal
