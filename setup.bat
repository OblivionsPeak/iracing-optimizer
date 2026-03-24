@echo off
echo ============================================
echo  iRacing Adaptive Settings Optimizer Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.12+ from python.org
    pause
    exit /b 1
)

:: Create venv
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

:: Activate and install
echo Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip --quiet
pip install -r requirements.txt

echo.
echo ============================================
echo  Setup complete! Run run.bat to start.
echo ============================================
pause
