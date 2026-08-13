@echo off
title Launching BSDC Ingest Engine...
echo ==================================================
echo  Starting FastAPI Engine ^& Opening n8n Workflow...
echo ==================================================

:: Auto-navigate to the script's actual directory
cd /d "%~dp0"

:: Activate Virtual Environment and run FastAPI in background
call venv\Scripts\activate
start /B python main.py

:: Wait 3 seconds for server readiness, then open browser
timeout /t 3 /nobreak >nul
start http://localhost:5678

echo.
echo Engine is running smoothly in background!