@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    python run.py %*
)
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Star Assistant exited with an error.
    pause
)
