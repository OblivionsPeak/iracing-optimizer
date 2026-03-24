@echo off
if not exist venv (
    echo Run setup.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
python app.py
pause
