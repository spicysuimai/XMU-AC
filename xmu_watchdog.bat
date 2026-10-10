@echo off
rem XMU-AC watchdog (zako-proven pattern): worker runs foreground, restart on exit
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
:loop
echo [%date% %time%] starting auto_checkin >> start_trace.txt
.venv\Scripts\python.exe launcher_worker.py >> checkin.log 2>&1
echo [%date% %time%] exited code=%ERRORLEVEL%, restart in 15s >> start_trace.txt
timeout /t 15 /nobreak >nul
goto loop
