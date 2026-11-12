# The "discord-music-bot" command on Windows. Run without arguments for help.
# -Pause (used by the Start menu shortcuts) keeps the window open at the end so its result can be read.
param([string]$Command = 'help', [Parameter(ValueFromRemainingArguments)] [string[]]$Rest = @(), [switch]$Elevated, [switch]$Pause)
$ErrorActionPreference = 'Stop'

$App = $PSScriptRoot
$Data = Join-Path $env:ProgramData 'Discord Music Bot'
$EnvFile = Join-Path $Data 'bot.env'
$Log = Join-Path $Data 'bot.log'
$TaskName = 'Discord Music Bot'
$Python = Join-Path $App 'python\python.exe'

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Need-Admin {
    # Re-run this command as administrator in a new window (Windows shows a permission prompt).
    if (Test-Admin) { return }
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $Command $($Rest -join ' ') -Elevated"
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit 0
}
function Done($code = 0) {
    if ($Elevated -or $Pause) { Read-Host "`nPress Enter to close" | Out-Null }
    exit $code
}

function Read-Shared($path, [long]$from = 0) {  # read a file the bot is still writing to
    if (-not (Test-Path $path)) { return '' }
    $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
        $fs.Seek([Math]::Min($from, $fs.Length), 'Begin') | Out-Null
        (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd()
    } finally { $fs.Dispose() }
}
function Log-Length { if (Test-Path $Log) { (Get-Item $Log).Length } else { 0 } }

function Set-Setting($key, $value) {  # replace the KEY= line in bot.env, keeping its locked-down permissions
    $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^$key=" }) + "$key=$value"
    [IO.File]::WriteAllText($EnvFile, ($lines -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding $false))
}

function Get-BotProcesses {  # the restart loop, launch.cmd, and everything run from the install folder
    Get-CimInstance Win32_Process | Where-Object {
        ($_.CommandLine -and ($_.CommandLine -like "*$App\run-bot.ps1*" -or $_.CommandLine -like "*$App\launch.cmd*")) -or
        ($_.ExecutablePath -and $_.ExecutablePath -like "$App\*")
    }
}

function Stop-Bot {
    # Leaves the task disabled: otherwise its restart-if-it-stops setting can bring the bot back mid-stop.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Stop-ScheduledTask -TaskName $TaskName
    }
    # Stopping the task ends only its first process, so end the bot, yt-dlp, FFmpeg and Deno too, and wait until
    # they're really gone: Windows keeps their files locked until then, which would break an upgrade.
    for ($i = 0; $i -lt 30; $i++) {
        $procs = @(Get-BotProcesses)
        if (-not $procs) { return }
        $procs | Sort-Object { $_.Name -notin 'powershell.exe', 'cmd.exe' } | ForEach-Object {  # restart loop first
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning "These bot processes didn't stop: $((Get-BotProcesses).Name -join ', ')"
}

function Start-Bot([switch]$Wait) {
    $from = Log-Length
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    if (-not $Wait) { return }
    Write-Host 'Starting the bot (usually a few seconds; up to 2 minutes right after a restart)...'
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep 1
        $new = Read-Shared $Log $from
        if ($new -match '(Logged in as [^\r\n]+)') {
            Write-Host $Matches[1] -ForegroundColor Green
            Write-Host 'The bot is running in the background and starts automatically when Windows starts.'
            return
        }
        if ($new -match 'LoginFailure|Improper token') {
            Write-Host "Discord rejected the token. Set it again with: discord-music-bot token" -ForegroundColor Red
            Stop-Bot; Done 1
        }
        if ($new -match 'Set DISCORD_TOKEN') {
            Write-Host 'No token is set yet. Set it with: discord-music-bot token' -ForegroundColor Yellow
            Stop-Bot; Done 1
        }
    }
    Write-Host "It hasn't logged in yet. Check the log: discord-music-bot logs" -ForegroundColor Yellow
}

switch ($Command) {
    'token' {
        Need-Admin
        Write-Host 'Get the token from https://discord.com/developers/applications -> your app -> Bot -> Reset Token.'
        $secure = Read-Host 'Paste the bot token (hidden while typing)' -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
        if ($token.Length -lt 50) { Write-Host "That doesn't look like a bot token (too short). Nothing changed." -ForegroundColor Red; Done 1 }
        $guild = (Read-Host 'Your server ID for instant command updates (optional, press Enter to skip)').Trim()
        Set-Setting DISCORD_TOKEN $token
        Set-Setting GUILD_ID $guild
        Stop-Bot; Start-Bot -Wait; Done
    }
    'set-setting' { Set-Setting $Rest[0] ($Rest[1..($Rest.Count)] -join ' ').Trim() }  # used by the installer
    'logs' {
        if (-not (Test-Path $Log)) { Write-Host 'No log yet: the bot has not started.'; Done }
        Write-Host "Showing $Log (Ctrl+C to quit)`n"
        Get-Content $Log -Tail 50 -Wait -Encoding UTF8
    }
    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) { Write-Host 'Not installed (no background task).'; Done 1 }
        $running = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$App\python\*" }).Count -gt 0
        Write-Host "Bot running:       $(if ($running) { 'yes' } else { 'no' })"
        Write-Host "Start with Windows: $(if ($task.State -ne 'Disabled') { 'yes' } else { 'no' })"
        $last = (Read-Shared $Log) -split "`r?`n" | Where-Object { $_ -match 'Logged in as|exited with code' } | Select-Object -Last 1
        if ($last) { Write-Host "Last event:        $last" }
        Done
    }
    'restart'  { Need-Admin; Stop-Bot; Start-Bot -Wait; Done }
    'start'    { Need-Admin; Start-Bot -Wait; Done }
    'stop' {
        Need-Admin; Stop-Bot
        Enable-ScheduledTask -TaskName $TaskName | Out-Null  # enabled again (not started), so it starts at next boot
        Write-Host 'Stopped. (It starts again when Windows starts; use "disable" to prevent that.)'; Done
    }
    'disable'  { Need-Admin; Stop-Bot; Disable-ScheduledTask -TaskName $TaskName | Out-Null; Write-Host "Stopped, and won't start with Windows."; Done }
    'enable'   { Need-Admin; Start-Bot -Wait; Done }
    'update-ytdlp' {
        Need-Admin
        & $Python -m pip install -q --upgrade --no-warn-script-location 'yt-dlp[default,curl-cffi]'
        Write-Host "yt-dlp is now $(& $Python -m yt_dlp --version)."
        Stop-Bot; Start-Bot -Wait; Done
    }
    'uninstall-cleanup' {  # used by the uninstaller
        Stop-Bot
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    default {
        @"
Usage: discord-music-bot <command>

  token          set the bot token (and optional server ID), then start the bot
  logs           follow the bot's live log (Ctrl+C to quit)
  status         show whether the bot is running
  restart        restart the bot
  stop | start   stop or start the bot
  disable        stop the bot and don't start it with Windows
  enable         start the bot and start it with Windows
  update-ytdlp   update yt-dlp (try this if songs stop loading), then restart

Commands that change things ask for administrator permission.
Settings: $EnvFile
Playlists and log: $Data
"@ | Write-Host
        Done
    }
}
