@echo off
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run_filewalk_batch.ps1" %*
exit /b %ERRORLEVEL%
