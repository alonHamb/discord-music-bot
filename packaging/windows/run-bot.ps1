# Started at boot by the "Discord Music Bot" scheduled task. Runs the bot, appends its output to bot.log, and
# restarts it if it exits (waiting longer each time if it keeps failing right away, e.g. with a bad token).
$App = $PSScriptRoot
$Data = Join-Path $env:ProgramData 'Discord Music Bot'
$Log = Join-Path $Data 'bot.log'

$env:BOT_ENV_FILE = Join-Path $Data 'bot.env'
$env:PLAYLISTS_FILE = Join-Path $Data 'playlists.json'
$env:FFMPEG_PATH = Join-Path $App 'ffmpeg\ffmpeg.exe'
$env:DENO_PATH = Join-Path $App 'deno\deno.exe'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'               # song titles in any language can be logged
$env:PYTHONDONTWRITEBYTECODE = '1'  # the install folder is read-only for the bot

$utf8 = New-Object Text.UTF8Encoding $false
function Note($text) { [IO.File]::AppendAllText($Log, "[$(Get-Date -Format s)] $text`r`n", $utf8) }

$delay = 10
while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 5MB) { Move-Item $Log "$Log.old" -Force }
    $started = Get-Date
    # launch.cmd does the redirection: PowerShell 5.1's own >> would write the log as UTF-16.
    $env:BOT_LOG = $Log
    & (Join-Path $App 'launch.cmd')
    $code = $LASTEXITCODE
    $delay = if (((Get-Date) - $started).TotalMinutes -gt 2) { 10 } else { [Math]::Min($delay * 2, 300) }
    Note "Bot exited with code $code; restarting in $delay seconds."
    Start-Sleep -Seconds $delay
}
