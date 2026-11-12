@echo off
rem Run the bot from this folder for development (no installer). See run.ps1 for what it does.
rem A batch file so it works even when PowerShell scripts are blocked on this PC.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
