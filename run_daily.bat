@echo off
REM Daily lead run. Starts Docker Desktop if needed, waits for it, then runs the pipeline.
cd /d "%~dp0"
docker info >nul 2>&1
if errorlevel 1 (
  echo Starting Docker Desktop...
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  :waitdocker
  timeout /t 10 /nobreak >nul
  docker info >nul 2>&1
  if errorlevel 1 goto waitdocker
)
if not exist logs mkdir logs
py run_daily.py >> logs\run_%date:~-4%-%date:~4,2%-%date:~7,2%.log 2>&1
