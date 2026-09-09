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

REM Start backend in a new window
start "OceanTrace API (port 8000)" cmd /k "cd /d %~dp0backend && python -m uvicorn oceantrace.api.main:app --host 0.0.0.0 --port 8000 --reload"

REM Wait 3 seconds for the backend to start
timeout /t 3 /nobreak > nul

REM Start frontend in a new window
start "OceanTrace Frontend (port 5173)" cmd /k "cd /d %~dp0apps\web && npm run dev"

echo  Both servers are starting...
echo  Press any key to open the browser.
pause > nul

start http://localhost:5173
