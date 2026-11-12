@echo off
rem The "discord-music-bot" command: forwards to manage.ps1 next to this file.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage.ps1" %*
