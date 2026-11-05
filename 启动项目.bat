@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo Starting Fashion Image Library...
start "Fashion Library API" /D "%ROOT%" "%PYTHON%" -m uvicorn api.main:app --host 0.0.0.0 --port 8520
if exist "%ROOT%frontend\node_modules" (
    start "Fashion Library Frontend" /D "%ROOT%frontend" cmd.exe /k npm.cmd run dev
) else (
    start "Fashion Library Frontend" /D "%ROOT%frontend" cmd.exe /k npm.cmd install ^&^& npm.cmd run dev
)

ping -n 9 127.0.0.1 >nul
start "" "http://127.0.0.1:8518"

endlocal