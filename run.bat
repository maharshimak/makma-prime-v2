@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python 3.11+ is required. Install Python and try again.
  pause
  exit /b 1
)
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo [MAK'MA] Created .env. Add your OPENAI_API_KEY, then run this file again.
  notepad .env
  pause
  exit /b 0
)
if not exist .venv (
  echo [MAK'MA] Creating local Python environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
start "" http://127.0.0.1:8765
python app.py
pause
