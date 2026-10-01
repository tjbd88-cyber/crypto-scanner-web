@echo off
cd /d "%~dp0"
curl.exe --silent --fail --max-time 2 http://127.0.0.1:8911/api/health >nul 2>&1
if not errorlevel 1 (
  echo Scanner service is already running at http://127.0.0.1:8911/
  exit /b 0
)
schtasks /Query /TN "\BottomReversalScanner" >nul 2>&1
if not errorlevel 1 (
  schtasks /Run /TN "\BottomReversalScanner" >nul 2>&1
  if errorlevel 1 (
    echo Could not start the background scanner. Check Windows Task Scheduler.
    pause
    exit /b 1
  )
  ping -n 5 127.0.0.1 >nul
  curl.exe --silent --fail --max-time 3 http://127.0.0.1:8911/api/health >nul 2>&1
  if errorlevel 1 (
    echo Background scanner did not become ready. Check Windows Task Scheduler.
    pause
    exit /b 1
  )
  echo Scanner service started. Open or refresh http://127.0.0.1:8911/
  exit /b 0
)
python -c "import fastapi, httpx, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo Installing Python dependencies for first run...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Check Python and network access.
    pause
    exit /b 1
  )
)
echo Scanner service starting. Open or refresh http://127.0.0.1:8911/
echo No background task is configured here. Keep this window open.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8911 --no-access-log
pause
