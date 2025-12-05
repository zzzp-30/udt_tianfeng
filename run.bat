@echo off
echo Starting main.py using uv...

cd udt_tianfeng

uv lock --upgrade-package akshare
uv run main.py

if %ERRORLEVEL% NEQ 0 (
    echo Error occurred while running main.py
    pause
    exit /b %ERRORLEVEL%
)

echo Application finished successfully.
pause