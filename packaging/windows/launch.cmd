@echo off
rem Runs the bot once, appending its output to %BOT_LOG%. Called in a loop by run-bot.ps1.
rem (A batch file because PowerShell 5.1 mangles quoted paths with spaces when building a cmd /c line.)
"%~dp0python\python.exe" "%~dp0bot.py" >> "%BOT_LOG%" 2>&1
