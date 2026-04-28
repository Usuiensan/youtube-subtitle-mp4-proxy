@echo off
setlocal

cd /d "%~dp0"

call "start-local-server.bat" -GpuEncode %*

endlocal
