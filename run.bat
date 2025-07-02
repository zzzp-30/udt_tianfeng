@echo off
echo Starting main.py using uv...

cd udt_tianfeng

uv run main.py --upgrade-package akshare

if %ERRORLEVEL% NEQ 0 (
    echo Error occurred while running main.py
    pause
    exit /b %ERRORLEVEL%
)

echo Application finished successfully.
pause