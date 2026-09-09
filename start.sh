#!/usr/bin/env bash
# OceanTrace AI — One-click startup script (Linux / macOS)
# Starts backend (FastAPI :8000) and frontend (Vite :5173).
# Run this file from the oceantrace-ai directory.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  ===================================================="
echo "    OceanTrace AI — Maritime Forensics DSS"
echo "    SIH 2026 PS 26143"
echo "  ===================================================="
echo ""
echo "  Starting backend API server on http://localhost:8000"
echo "  Starting frontend dev server on http://localhost:5173"
echo ""
echo "  Open http://localhost:5173 in your browser to use the app."
echo "  API docs available at http://localhost:8000/api/docs"
echo ""

# ---- Backend ----
cd "$DIR/backend"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "  [setup] Creating Python virtual environment..."
  python3 -m venv .venv
fi

if [ ! -f "../.env" ]; then
  echo "  [setup] Initializing .env file..."
  cp ../.env.example ../.env
fi

# Activate and install deps
source .venv/bin/activate
echo "  [setup] Installing Python dependencies..."
pip install -q -r requirements.txt

echo "  [start] Launching backend..."
python -m uvicorn oceantrace.api.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

# ---- Frontend ----
cd "$DIR/apps/web"

# Install node modules if needed
if [ ! -d "node_modules" ]; then
  echo "  [setup] Installing frontend dependencies..."
  npm install
fi

echo "  [start] Launching frontend..."
npm run dev &
FRONTEND_PID=$!

# ---- Cleanup on exit ----
cleanup() {
  echo ""
  echo "  Shutting down..."
  kill $BACKEND_PID 2>/dev/null || true
  kill $FRONTEND_PID 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

echo ""
echo "  Both servers are running."
echo "  Press Ctrl+C to stop both."
echo ""

# Wait for either process
wait
