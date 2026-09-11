@echo off
cd /d "%~dp0"
python run.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Star Assistant exited with an error.
    pause
)
