@echo off
setlocal
title POS Leads - getting today's leads
cd /d "%~dp0"
docker info >nul 2>&1
if not errorlevel 1 goto run
echo Starting Docker Desktop, please wait...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
set /a tries=0
:wait
timeout /t 10 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto run
set /a tries+=1
if %tries% lss 60 goto wait
echo Docker did not start. Open Docker Desktop yourself, wait for "Engine running", then run this again.
pause
exit /b 1
:run
echo.
echo Finding leads. This can take 30 to 60 minutes. Keep this window open.
echo.
py run_daily.py
echo.
echo Finished. Opening your Google Sheet...
start "" "https://docs.google.com/spreadsheets/d/1g_MgGOYr7KiNVNweYGeffa6Gujf0lW6aSr8nOXyrB3c/edit"
pause
