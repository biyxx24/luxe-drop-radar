@echo off
rem Luxury new-arrivals monitor - Windows launcher. Restarts automatically if it crashes.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul && (set PY=python) || (set PY=py -3)
:loop
%PY% monitor.py run
echo Monitor stopped. Restarting in 60 seconds... (close this window to quit)
timeout /t 60 /nobreak >nul
goto loop
