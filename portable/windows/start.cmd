@echo off
rem Set up (first time) and run the Discord music bot from this folder. No installer needed.
rem   start.cmd                  run the bot
rem   start.cmd --setup-only     only set up
rem   start.cmd --autostart on   also start it when you log in (--autostart off undoes it)
setlocal
cd /d "%~dp0"
title Discord Music Bot

rem Find Python 3.10+: the "py" launcher first, then "python" (skipping the Microsoft Store placeholder).
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3.10 or newer is needed. Install it, then run start.cmd again:
    echo   winget install Python.Python.3.13
    echo or download it from https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY% "%~dp0launcher.py" %*
if errorlevel 1 pause
