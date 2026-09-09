@echo off
REM OceanTrace AI — One-click startup script
REM Starts backend (FastAPI :8000) and frontend (Vite :5173) in separate windows.
REM Run this file from the oceantrace-ai directory.

echo.
echo  ====================================================
echo    OceanTrace AI — Maritime Forensics DSS
echo    SIH 2026 PS 26143
echo  ====================================================
echo.
echo  Starting backend API server on http://localhost:8000
echo  Starting frontend dev server on http://localhost:5173
echo.
echo  Open http://localhost:5173 in your browser to use the app.
echo  API docs available at http://localhost:8000/api/docs
echo.

REM Start backend in a new window (auto-setup)
start "OceanTrace API (port 8000)" cmd /k "cd /d %~dp0backend & if not exist .venv (echo [setup] Creating Python venv... & python -m venv .venv) & call .venv\Scripts\activate & echo [setup] Installing Python dependencies... & pip install -q -r requirements.txt & if not exist ..\.env (copy ..\.env.example ..\.env) & echo [start] Launching backend... & python -m uvicorn oceantrace.api.main:app --host 0.0.0.0 --port 8000 --reload"

REM Wait 3 seconds for the backend to start
timeout /t 3 /nobreak > nul

REM Start frontend in a new window (auto-setup)
start "OceanTrace Frontend (port 5173)" cmd /k "cd /d %~dp0apps\web & if not exist node_modules (echo [setup] Installing frontend dependencies... & npm install) & echo [start] Launching frontend... & npm run dev"

echo  Both servers are starting...
echo  Press any key to open the browser.
pause > nul

start http://localhost:5173
