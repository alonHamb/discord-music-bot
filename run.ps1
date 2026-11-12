<#
.SYNOPSIS
Run the bot from this folder for development: no installer, no package. Use run.cmd to start it (that also works
when PowerShell scripts are blocked on this PC).

.DESCRIPTION
Sets up whatever is missing, then starts the bot in this window (Ctrl+C stops it):
  - the Python environment in .venv, and its packages (reinstalled only when requirements.txt changes)
  - the bot token in .env (asks for it)
  - FFmpeg and Deno (offers to install them with winget)
It also checks that no other copy of the bot is running: two copies with the same token both answer commands.

.EXAMPLE
.\run.cmd               # set up if needed, then run
.\run.cmd -SetupOnly    # set up, but don't start the bot
#>
param([switch]$SetupOnly)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$EnvFile = Join-Path $Root '.env'
$TaskName = 'Discord Music Bot'

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "Error: $text" -ForegroundColor Red; exit 1 }
function Ask-Yes($question) { (Read-Host "$question [Y/n]").Trim() -notmatch '^(n|no)$' }
function Ask-No($question) { (Read-Host "$question [y/N]").Trim() -match '^(y|yes)$' }

# --- Python environment ------------------------------------------------------------------------------------------
function Native($exe, [string[]]$arguments) {  # run a program, showing its output; returns its exit code
    $ErrorActionPreference = 'Continue'  # its stderr warnings aren't PowerShell errors
    & $exe @arguments | Out-Host
    $LASTEXITCODE
}

if (-not (Test-Path $Python)) {
    Step 'Creating the Python environment (.venv)'
    $base = $null
    foreach ($candidate in @(@{ Exe = 'py'; Args = @('-3') }, @{ Exe = 'python'; Args = @() })) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $check = $candidate.Args + @('-c', 'import sys; sys.exit(sys.version_info < (3, 10))')
        if ((Native $candidate.Exe $check ) -eq 0) { $base = $candidate; break }
    }
    if (-not $base) { Fail 'Python 3.10 or newer is needed. Install it with:  winget install Python.Python.3.13' }
    if ((Native $base.Exe ($base.Args + @('-m', 'venv', $Venv)) ) -ne 0) {
        Fail 'Creating the Python environment failed.'
    }
}

$stamp = Join-Path $Venv '.requirements.sha256'
$hash = (Get-FileHash (Join-Path $Root 'requirements.txt')).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Step "Installing the bot's Python packages"
    $pip = @('-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--upgrade', '-r', (Join-Path $Root 'requirements.txt'))
    if ((Native $Python $pip ) -ne 0) { Fail 'Installing the packages failed (see above).' }
    Set-Content $stamp $hash  # next run skips this until requirements.txt changes
}

# --- Bot token -----------------------------------------------------------------------------------------------------
$lines = if (Test-Path $EnvFile) { @(Get-Content $EnvFile) } else { @(Get-Content (Join-Path $Root '.env.example')) }
$token = ($lines | Where-Object { $_ -match '^DISCORD_TOKEN=' }) -replace '^DISCORD_TOKEN=', ''
if ("$token".Trim().Length -lt 50) {
    Step 'Bot token'
    Write-Host 'Get it from https://discord.com/developers/applications -> your app -> Bot -> Reset Token.'
    $secure = Read-Host 'Paste the bot token (hidden while typing)' -AsSecureString
    $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
    if ($token.Length -lt 50) { Fail "That doesn't look like a bot token (too short)." }
    $guild = (Read-Host 'Your server ID for instant command updates (optional, press Enter to skip)').Trim()
    $lines = @($lines | Where-Object { $_ -notmatch '^(DISCORD_TOKEN|GUILD_ID)=' }) + "DISCORD_TOKEN=$token" + "GUILD_ID=$guild"
    [IO.File]::WriteAllLines($EnvFile, $lines, (New-Object Text.UTF8Encoding $false))
    Write-Host 'Saved in .env (Git ignores this file, so the token stays private).'
}

# --- FFmpeg and Deno: bot.py finds them on PATH or where winget puts them -------------------------------------------
$winget = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet'
function Has-Tool($name) {
    [bool](Get-Command $name -ErrorAction SilentlyContinue) -or (Test-Path "$winget\Links\$name.exe") -or
        [bool](Get-ChildItem "$winget\Packages" -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
}
foreach ($tool in @(@('ffmpeg', 'Gyan.FFmpeg', 'plays the audio'), @('deno', 'DenoLand.Deno', 'YouTube needs it'))) {
    if (Has-Tool $tool[0]) { continue }
    Write-Host "$($tool[0]) is missing ($($tool[2]))." -ForegroundColor Yellow
    if (-not (Ask-Yes "Install it with winget now?")) { Fail "Install it with:  winget install $($tool[1])" }
    $null = Native winget @('install', '--id', $tool[1], '-e', '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
    if (-not (Has-Tool $tool[0])) { Fail "Installing $($tool[0]) failed." }
}

if ($SetupOnly) { Write-Host 'Set up. Start the bot with:  .\run.cmd' -ForegroundColor Green; exit 0 }

# --- Other copies of the bot ---------------------------------------------------------------------------------------
# Normal users can't see the installed bot's task or processes (they belong to LOCAL SERVICE), but a Python
# process whose path is hidden from us, with the bot installed, is that bot.
$task = try { Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { $null }
$installed = Test-Path (Join-Path $env:ProgramFiles 'Discord Music Bot\run-bot.ps1')
$hiddenPython = @(Get-Process python -ErrorAction SilentlyContinue | Where-Object { -not $_.Path })
if (($task -and $task.State -eq 'Running') -or ($installed -and $hiddenPython)) {
    Write-Host 'The installed bot (from Setup.exe) is running with the same token. Two copies both answer commands.' -ForegroundColor Yellow
    Write-Host 'Stop it with:  Start menu -> Discord Music Bot -> Stop bot   (or: discord-music-bot stop)'
    if (-not (Ask-No 'Run this copy anyway?')) { exit 1 }
}
$others = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'bot\.py' -and $_.ExecutablePath -like "$Venv\*" })
if ($others) {
    Write-Host "Another copy of the bot is already running from this folder (process $($others.ProcessId -join ', '))." -ForegroundColor Yellow
    if (Ask-Yes 'Stop it and start this one?') { $others | ForEach-Object { Stop-Process -Id $_.ProcessId -Force } }
    else { exit 1 }
}

# --- Run ------------------------------------------------------------------------------------------------------------
Step 'Starting the bot (Ctrl+C to stop)'
$env:PYTHONUNBUFFERED = '1'  # show its output immediately, also when it's redirected to a file
& $Python (Join-Path $Root 'bot.py')
exit $LASTEXITCODE
