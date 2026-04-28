@echo off
setlocal

cd /d "%~dp0"

echo [youtube-mp4-proxy] Preparing local settings...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\reset-local-env.ps1"
if errorlevel 1 goto error

if not exist ".venv\Scripts\python.exe" (
  echo [youtube-mp4-proxy] Creating virtual environment...
  py -3.12 -m venv .venv
  if errorlevel 1 py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
  if errorlevel 1 goto error
)

echo [youtube-mp4-proxy] Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -U pip
if errorlevel 1 goto error
".venv\Scripts\python.exe" -m pip install -r requirements.txt "yt-dlp[default]"
if errorlevel 1 goto error

where deno >nul 2>nul
if errorlevel 1 (
  echo [youtube-mp4-proxy] Warning: deno was not found in PATH.
  echo [youtube-mp4-proxy] YouTube downloads may fail until Deno is installed or YTDLP_EXTRA_ARGS is changed.
) else (
  for /f "delims=" %%i in ('deno --version 2^>nul') do (
    echo [youtube-mp4-proxy] %%i
    goto deno_done
  )
)
:deno_done

echo.
echo [youtube-mp4-proxy] Starting server...
echo [youtube-mp4-proxy] Open http://127.0.0.1:8000/
echo [youtube-mp4-proxy] Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
goto end

:error
echo.
echo [youtube-mp4-proxy] Failed to start. See the error above.
pause

:end
endlocal
