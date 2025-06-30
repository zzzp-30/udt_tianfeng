@echo off
echo Starting main.py using uv...

if not exist ".venv" (
    echo Error: .venv virtual environment not found!
    echo Please create the virtual environment first.
    pause
    exit /b 1
)

uv run udt\main.py

if %ERRORLEVEL% NEQ 0 (
    echo Error occurred while running udt\main.py
    pause
    exit /b %ERRORLEVEL%
)

echo Application finished successfully.
pause