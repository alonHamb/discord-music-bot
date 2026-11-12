@echo off
rem Publish a new version as a GitHub release in one step. See release.ps1 for what it does.
rem A batch file so it works even when PowerShell scripts are blocked on this PC.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
