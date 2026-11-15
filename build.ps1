<#
.SYNOPSIS
Build installers for the Discord music bot into the dist folder.

.EXAMPLE
.\build.ps1                      # asks which installer(s) and which version
.\build.ps1 windows 1.0.1            # Windows Setup.exe only
.\build.ps1 windows-portable 1.0.1   # Windows portable zip only (no installer: unzip, run start.cmd)
.\build.ps1 linux 1.0.1              # Linux .deb only
.\build.ps1 linux-portable 1.0.1     # Linux portable .tar.gz only (no package: extract, run ./start.sh)
.\build.ps1 all 1.0.1                # all four
.\build.ps1 all 1.0.1 -Yes           # all four, and delete older installers without asking

Every .py file in this folder plus requirements.txt goes into the installers. Building a version that's already in
dist overwrites it; after a successful build, older versions' installers (for the platforms just built) are deleted
once you confirm. The Windows build needs Inno Setup (winget install JRSoftware.InnoSetup); the Linux builds need
WSL with Debian or Ubuntu.
#>
param(
    [ValidateSet('windows', 'windows-portable', 'linux', 'linux-portable', 'all')] [string]$Target,
    [string]$Version,
    [string]$WslDistro = 'Debian',
    [switch]$Yes  # delete older installers without asking
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Dist = Join-Path $Root 'dist'
$VersionFile = Join-Path $Root 'VERSION'

function Fail($msg) { Write-Host "Error: $msg" -ForegroundColor Red; exit 1 }

# --- What to build ---------------------------------------------------------------------------------------------
if (-not $Target) {
    Write-Host ("Which installer do you want to build?`n  1) Windows (Setup.exe)`n  2) Windows portable (zip, no installer)`n" +
                "  3) Linux (.deb for Debian/Ubuntu/Raspberry Pi OS)`n  4) Linux portable (.tar.gz, no package)`n  5) All four")
    $Target = switch (Read-Host 'Choose 1-5') {
        '1' { 'windows' } '2' { 'windows-portable' } '3' { 'linux' } '4' { 'linux-portable' } '5' { 'all' }
        default { Fail 'Please choose 1, 2, 3, 4 or 5.' }
    }
}

$last = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { '' }
if (-not $Version) {
    $suggest = if ($last -match '^(\d+)\.(\d+)\.(\d+)$') { "$($Matches[1]).$($Matches[2]).$([int]$Matches[3] + 1)" } else { '1.0.0' }
    $prompt = if ($last) { "Version (last build was $last) [$suggest]" } else { "Version [$suggest]" }
    $Version = (Read-Host $prompt).Trim()
    if (-not $Version) { $Version = $suggest }
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version must look like 1.2.3 (got '$Version')." }
if ($last -and $Version -eq $last) {
    Write-Host "Note: $Version was already built. Installed copies only upgrade to a HIGHER version." -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force $Dist | Out-Null

# --- Older installers in dist --------------------------------------------------------------------------------------
# Only for the platforms being built. The same version's installer isn't "old": the build simply overwrites it.
$installers = @{
    'windows'          = @{ Pattern = 'discord-music-bot-*-windows-setup.exe';     This = "discord-music-bot-$Version-windows-setup.exe" }
    'windows-portable' = @{ Pattern = 'discord-music-bot-*-windows-portable.zip';  This = "discord-music-bot-$Version-windows-portable.zip" }
    'linux'            = @{ Pattern = 'discord-music-bot_*_all.deb';               This = "discord-music-bot_${Version}_all.deb" }
    'linux-portable'   = @{ Pattern = 'discord-music-bot-*-linux-portable.tar.gz'; This = "discord-music-bot-$Version-linux-portable.tar.gz" }
}
$platforms = if ($Target -eq 'all') { 'windows', 'windows-portable', 'linux', 'linux-portable' } else { @($Target) }
$older = @(foreach ($p in $platforms) {
    Get-ChildItem $Dist -Filter $installers[$p].Pattern | Where-Object Name -ne $installers[$p].This
})
foreach ($p in $platforms) {
    if (Test-Path (Join-Path $Dist $installers[$p].This)) { Write-Host "dist\$($installers[$p].This) exists and will be overwritten." }
}
$deleteOlder = $false
if ($older) {
    Write-Host "`nOlder installers in dist:"
    $older | ForEach-Object { Write-Host "  $($_.Name)" }
    $deleteOlder = $Yes -or ((Read-Host 'Delete them once the new build succeeds? [Y/n]').Trim() -notmatch '^(n|no)$')
}

# --- Windows -----------------------------------------------------------------------------------------------------
function Build-Windows {
    Write-Host "`n==> Building the Windows installer $Version" -ForegroundColor Cyan
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { Fail 'Inno Setup is not installed. Install it with:  winget install JRSoftware.InnoSetup' }

    $stage = Join-Path $env:TEMP ("dmb-stage-" + [guid]::NewGuid())
    New-Item -ItemType Directory $stage | Out-Null
    try {
        Copy-Item (Join-Path $Root '*.py'), (Join-Path $Root 'requirements.txt') $stage
        Copy-Item (Join-Path $Root 'packaging\windows\*') $stage -Exclude 'installer.iss'
        # Batch files need Windows line endings.
        Get-ChildItem $stage -Filter *.cmd | ForEach-Object {
            $text = [IO.File]::ReadAllText($_.FullName) -replace "`r?`n", "`r`n"
            [IO.File]::WriteAllText($_.FullName, $text)
        }
        & $iscc /Q "/DAppVersion=$Version" "/DStageDir=$stage" "/O$Dist" (Join-Path $Root 'packaging\windows\installer.iss')
        if ($LASTEXITCODE -ne 0) { Fail 'Inno Setup failed (see the messages above).' }
    } finally { Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue }
    $out = Join-Path $Dist "discord-music-bot-$Version-windows-setup.exe"
    Write-Host "Built $out ($([Math]::Round((Get-Item $out).Length / 1KB)) KB)" -ForegroundColor Green
}

# --- Windows portable (no installer) ------------------------------------------------------------------------------
function Build-WindowsPortable {
    Write-Host "`n==> Building the Windows portable zip $Version" -ForegroundColor Cyan
    $name = "discord-music-bot-$Version-windows-portable"
    $stage = Join-Path $env:TEMP ("dmb-portable-" + [guid]::NewGuid())
    $folder = Join-Path $stage $name  # the zip holds this one folder, so unzipping doesn't scatter files
    New-Item -ItemType Directory $folder | Out-Null
    try {
        Copy-Item (Join-Path $Root '*.py'), (Join-Path $Root 'requirements.txt'), (Join-Path $Root '.env.example') $folder
        Copy-Item (Join-Path $Root 'portable\launcher.py'), (Join-Path $Root 'portable\windows\*') $folder
        Set-Content (Join-Path $folder 'VERSION') $Version -NoNewline
        # Batch files need Windows line endings; README.txt is read in Notepad.
        Get-ChildItem $folder -Include *.cmd, *.txt -File -Recurse | ForEach-Object {
            $text = [IO.File]::ReadAllText($_.FullName) -replace "`r?`n", "`r`n"
            [IO.File]::WriteAllText($_.FullName, $text)
        }
        $out = Join-Path $Dist "$name.zip"
        # Zip entry by entry with "/" in the paths: Windows PowerShell's Compress-Archive (and .NET's zip helpers
        # there) write "\", which other unzip tools turn into odd file names.
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        Remove-Item $out -ErrorAction SilentlyContinue
        $zip = [IO.Compression.ZipFile]::Open($out, 'Create')
        try {
            Get-ChildItem $folder -File -Recurse -Force | ForEach-Object {
                $entry = $name + '/' + $_.FullName.Substring($folder.Length + 1).Replace('\', '/')
                [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $entry)
            }
        } finally { $zip.Dispose() }
    } finally { Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue }
    Write-Host "Built $out ($([Math]::Round((Get-Item $out).Length / 1KB)) KB)" -ForegroundColor Green
}

# --- Linux (both made in WSL: Linux tools set the file permissions right) ---------------------------------------------
function Invoke-InWsl($script, $what) {
    $distros = (wsl.exe -l -q 2>$null | Out-String) -replace "`0", '' -split "`r?`n" | Where-Object { $_.Trim() }
    if ($distros -notcontains $WslDistro) {
        Fail "WSL distro '$WslDistro' not found (have: $($distros -join ', ')). Use -WslDistro <name> with a Debian or Ubuntu one."
    }
    $path = (wsl.exe -d $WslDistro -- wslpath -a ((Join-Path $Root "packaging\$script") -replace '\\', '/') | Out-String).Trim()
    wsl.exe -d $WslDistro -- bash $path $Version
    if ($LASTEXITCODE -ne 0) { Fail "Building the $what failed (see the messages above)." }
}
function Build-Linux {
    Write-Host "`n==> Building the Linux package $Version (in WSL $WslDistro)" -ForegroundColor Cyan
    Invoke-InWsl 'build-deb.sh' '.deb'
}
function Build-LinuxPortable {
    Write-Host "`n==> Building the Linux portable .tar.gz $Version (in WSL $WslDistro)" -ForegroundColor Cyan
    Invoke-InWsl 'build-linux-portable.sh' 'Linux portable .tar.gz'
}

if ($Target -in 'windows', 'all') { Build-Windows }
if ($Target -in 'windows-portable', 'all') { Build-WindowsPortable }
if ($Target -in 'linux', 'all') { Build-Linux }
if ($Target -in 'linux-portable', 'all') { Build-LinuxPortable }
Set-Content -Path $VersionFile -Value $Version -NoNewline
if ($deleteOlder) {  # only reached when every build succeeded (failures exit above)
    $older | Remove-Item -Force
    Write-Host "Deleted $($older.Count) older installer(s): $(($older.Name) -join ', ')"
}
Write-Host "`nDone. Installers are in $Dist" -ForegroundColor Green
