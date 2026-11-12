# Run by the Windows installer (as administrator) after copying the bot's files.
# Puts a private Python, FFmpeg and Deno inside the install folder, installs the bot's packages, locks down the
# settings, and registers a scheduled task that runs the bot in the background from boot as LOCAL SERVICE.
param(
    [string]$TokenFile  # optional: file with the token on line 1 and the server ID on line 2 (deleted after reading)
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$App = $PSScriptRoot
$Data = Join-Path $env:ProgramData 'Discord Music Bot'
$EnvFile = Join-Path $Data 'bot.env'
$TaskName = 'Discord Music Bot'
$Python = Join-Path $App 'python\python.exe'
$PythonMinor = '3.13'

New-Item -ItemType Directory -Force $Data | Out-Null
$InstallLog = Join-Path $Data 'install.log'
try { Start-Transcript -Path $InstallLog -Force | Out-Null }
catch {  # install.log still open (e.g. an earlier, interrupted install): log to a separate file instead of failing
    $InstallLog = Join-Path $Data "install-$(Get-Date -Format yyyyMMdd-HHmmss).log"
    Start-Transcript -Path $InstallLog -Force | Out-Null
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Download([string[]]$urls, $file) {
    # Windows' built-in curl: much faster than Invoke-WebRequest, and it gives up on a stalled server (under
    # 50 KB/s for 30 s) instead of waiting forever. Tries each URL in turn, each up to 3 times.
    foreach ($url in $urls) {
        Write-Host "    $url"
        & curl.exe -fsSL --retry 3 --retry-delay 3 --connect-timeout 30 --speed-limit 51200 --speed-time 30 -o $file $url
        if ($LASTEXITCODE -eq 0) { return }
        Write-Host "    (failed with curl exit code $LASTEXITCODE)"
    }
    throw "Download failed: $($urls -join ' or ')"
}
function Url-Exists($url) { & curl.exe -fsIL --connect-timeout 30 --max-time 60 -o NUL $url; $LASTEXITCODE -eq 0 }
function Extract-One($zip, $name, $dest) {  # copy one file out of a zip, wherever it is inside
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $entry = $archive.Entries | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $entry) { throw "$name not found in $zip" }
        New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
    } finally { $archive.Dispose() }
}
function Run($exe) {  # run a program with its output in the log; fail loudly if it fails
    $ErrorActionPreference = 'Continue'  # its stderr lines (e.g. pip warnings) aren't errors
    & $exe @args 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $exe $args" }
}
$Tmp = Join-Path $env:TEMP ("dmb-" + [guid]::NewGuid())
New-Item -ItemType Directory $Tmp | Out-Null

try {
    # A running bot locks its files (Python packages, FFmpeg, Deno), so stop it before replacing anything.
    # The task is registered again below.
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Step 'Stopping the running bot'
        & (Join-Path $App 'manage.ps1') uninstall-cleanup
    }

    # --- Settings folder: only SYSTEM, Administrators and the bot (LOCAL SERVICE) can change it; the token file
    #     is readable only by them, never by normal users. ---------------------------------------------------------
    Step 'Preparing the settings folder'
    if (-not (Test-Path $EnvFile)) {
        $template = "# Discord music bot settings. Set the token with:  discord-music-bot token`r`nDISCORD_TOKEN=`r`n" +
                    "# Optional: your server ID, so new slash commands appear instantly`r`nGUILD_ID=`r`n"
        [IO.File]::WriteAllText($EnvFile, $template, (New-Object Text.UTF8Encoding $false))
    }
    Run icacls $Data /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    Run icacls $EnvFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-19:R' | Out-Null

    if ($TokenFile -and (Test-Path $TokenFile)) {
        $lines = @(Get-Content $TokenFile) + @('', '')
        Remove-Item $TokenFile -Force
        & (Join-Path $App 'manage.ps1') set-setting DISCORD_TOKEN $lines[0].Trim()
        & (Join-Path $App 'manage.ps1') set-setting GUILD_ID $lines[1].Trim()
    }

    # --- Python: the official "embeddable" build, private to this folder ------------------------------------------
    if (-not (Test-Path $Python)) {
        Step "Downloading Python $PythonMinor"
        $index = (& curl.exe -fsSL --connect-timeout 30 --max-time 60 'https://www.python.org/ftp/python/') -join "`n"
        $patches = [regex]::Matches($index, "href=`"$([regex]::Escape($PythonMinor))\.(\d+)/`"") |
            ForEach-Object { [int]$_.Groups[1].Value } | Sort-Object -Descending -Unique
        $zipUrl = $null
        foreach ($p in $patches) {  # newest patch release that has a Windows build
            $url = "https://www.python.org/ftp/python/$PythonMinor.$p/python-$PythonMinor.$p-embed-amd64.zip"
            if (Url-Exists $url) { $zipUrl = $url; break }
        }
        if (-not $zipUrl) { throw "Couldn't find a Python $PythonMinor download on python.org" }
        Download $zipUrl "$Tmp\python.zip"
        Expand-Archive "$Tmp\python.zip" (Join-Path $App 'python') -Force
        # Let it use site-packages (for pip) and find the bot's own modules in the folder above.
        $pth = Get-ChildItem (Join-Path $App 'python') -Filter 'python*._pth' | Select-Object -First 1
        $lines = (Get-Content $pth.FullName) -replace '^#\s*import site', 'import site'
        [IO.File]::WriteAllLines($pth.FullName, @($lines + '..'), (New-Object Text.UTF8Encoding $false))
        Step 'Installing pip'
        Download 'https://bootstrap.pypa.io/get-pip.py' "$Tmp\get-pip.py"
        Run $Python "$Tmp\get-pip.py" --no-warn-script-location -q
    }

    Step "Installing the bot's Python packages (can take a few minutes)"
    Run $Python -m pip install -q --upgrade --no-warn-script-location pip
    Run $Python -m pip install -q --upgrade --no-warn-script-location -r (Join-Path $App 'requirements.txt')

    # --- FFmpeg and Deno -------------------------------------------------------------------------------------------
    $ffmpeg = Join-Path $App 'ffmpeg\ffmpeg.exe'
    if (-not (Test-Path $ffmpeg)) {
        Step 'Downloading FFmpeg'
        Download @('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip',
                   'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip') "$Tmp\ffmpeg.zip"
        Extract-One "$Tmp\ffmpeg.zip" 'ffmpeg.exe' $ffmpeg
    }
    $deno = Join-Path $App 'deno\deno.exe'
    if (-not (Test-Path $deno)) {
        Step 'Downloading Deno (YouTube needs it)'
        Download 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' "$Tmp\deno.zip"
        Extract-One "$Tmp\deno.zip" 'deno.exe' $deno
    }

    Step 'Checking the bot loads'
    Push-Location $App
    try { Run $Python -c 'import bot' } finally { Pop-Location }

    # --- Background task: at boot, as LOCAL SERVICE (a restricted built-in account), restarts if it stops ---------
    Step 'Registering the background task'
    $account = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-19').Translate([Security.Principal.NTAccount]).Value
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $Data -Argument (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $App 'run-bot.ps1')`"")
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Settings $settings `
        -Trigger (New-ScheduledTaskTrigger -AtStartup) `
        -Principal (New-ScheduledTaskPrincipal -UserId $account -LogonType ServiceAccount -RunLevel Limited) | Out-Null

    $hasToken = Select-String -Path $EnvFile -Pattern '^DISCORD_TOKEN=\S{50,}' -Quiet
    if ($hasToken) {
        Step 'Starting the bot'
        & (Join-Path $App 'manage.ps1') restart
    } else {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null  # nothing to run until there's a token
        Write-Host "`nInstalled. Set your bot token to start it: Start menu -> Discord Music Bot -> Set bot token" -ForegroundColor Yellow
    }
    Write-Host "`nDone." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "`nInstallation failed: $_" -ForegroundColor Red
    Write-Host "Full log: $InstallLog"
    exit 1
}
finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    Stop-Transcript | Out-Null
}
