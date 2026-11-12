<#
.SYNOPSIS
Publish a new version as a GitHub release in one step. Start it with release.cmd.

.DESCRIPTION
  1. Checks: on main, everything committed and in sync with GitHub, the version is new and higher than the last
     release, the GitHub CLI is logged in.
  2. Sets VERSION, commits it, and tags v<version>.
  3. Builds the Windows and Linux installers from that tag, in a clean folder (nothing local gets in).
  4. Checks the installers for private information: your bot token and server ID, your real email addresses,
     your user folder and your PC's name (all read from this PC at run time).
  5. Shows a summary and asks before publishing. If you say no, or anything fails before publishing, the commit
     and tag are undone.
  6. Pushes the commit and tag, creates the GitHub release with notes and checksums, and checks the downloads.

Needs Inno Setup and WSL with Debian or Ubuntu (like build.ps1), and the GitHub CLI (gh) logged in.

.EXAMPLE
.\release.cmd                 # asks for the version (suggests the next one)
.\release.cmd 1.0.4           # release 1.0.4
.\release.cmd 1.0.4 -DryRun   # do everything except publish, then undo the commit and tag
#>
param(
    [string]$Version,
    [switch]$DryRun,
    [string]$WslDistro = 'Debian'
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nError: $text" -ForegroundColor Red; exit 1 }
function RunGit([string[]]$arguments) {  # run git; stop on failure; return its output
    $ErrorActionPreference = 'Continue'
    $out = & git @arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($arguments -join ' ') failed: $($out -join ' ')" }
    $out | ForEach-Object { "$_" }
}
function Quiet([scriptblock]$command) {  # run a native command; $true if it succeeded
    $ErrorActionPreference = 'Continue'
    & $command *> $null
    $LASTEXITCODE -eq 0
}
function VersionKey($v) { $p = $v -split '\.'; [int]$p[0] * 1000000 + [int]$p[1] * 1000 + [int]$p[2] }

# --- 1. Checks --------------------------------------------------------------------------------------------------
Step 'Checking that everything is ready'
if (-not (Quiet { gh auth status })) { Fail 'The GitHub CLI is not logged in. Run:  gh auth login' }
$repo = (& gh repo view --json nameWithOwner --jq .nameWithOwner 2>$null)
if (-not $repo) { Fail "This folder isn't connected to a GitHub repository." }
$branch = RunGit @('branch', '--show-current')
if ($branch -ne 'main') { Fail "You're on branch '$branch'. Releases are made from main:  git checkout main" }
if (RunGit @('status', '--porcelain')) { Fail 'There are uncommitted changes. Commit (or stash) them first, then run this again.' }
RunGit @('fetch', '-q', '--tags', 'origin') | Out-Null
$behind = [int](RunGit @('rev-list', '--count', 'HEAD..origin/main'))
if ($behind) { Fail "GitHub has $behind commit(s) that this PC doesn't. Get them first:  git pull" }

$tags = @(RunGit @('tag', '-l', 'v*') | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } | Sort-Object { VersionKey $_.Substring(1) })
$lastTag = $tags | Select-Object -Last 1
$last = if ($lastTag) { $lastTag.Substring(1) } else { '' }
if (-not $Version) {
    $suggest = if ($last -match '^(\d+)\.(\d+)\.(\d+)$') { "$($Matches[1]).$($Matches[2]).$([int]$Matches[3] + 1)" } else { '1.0.0' }
    $Version = "$(Read-Host "Version to release (last release: $(if ($last) { $last } else { 'none' })) [$suggest]")".Trim()
    if (-not $Version) { $Version = $suggest }
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version must look like 1.2.3 (got '$Version')." }
$Tag = "v$Version"
if ($tags -contains $Tag -or (RunGit @('ls-remote', '--tags', 'origin', $Tag))) { Fail "$Tag already exists. Pick a higher version." }
if ($last -and (VersionKey $Version) -le (VersionKey $last)) { Fail "$Version isn't higher than the last release ($last)." }
Write-Host "Releasing $Version (last release: $(if ($last) { $last } else { 'none' }))$(if ($DryRun) { ' - DRY RUN, nothing will be published' })"

# --- 2. Version commit and tag (undone if we stop before publishing) ----------------------------------------------
$startCommit = RunGit @('rev-parse', 'HEAD')
$script:madeCommit = $false; $script:madeTag = $false; $published = $false; $failed = $false
$work = Join-Path $env:TEMP ("dmb-release-" + [guid]::NewGuid())
function Undo {  # safe to call more than once
    if ($script:madeTag) { RunGit @('tag', '-d', $Tag) | Out-Null; $script:madeTag = $false; $undid = $true }
    if ($script:madeCommit) { RunGit @('reset', '-q', '--keep', $startCommit) | Out-Null; $script:madeCommit = $false; $undid = $true }
    if ($undid) { Write-Host "Undid the version commit and tag $Tag; nothing was published." -ForegroundColor Yellow }
}

try {
    Step "Setting VERSION to $Version and tagging $Tag"
    if ((Get-Content (Join-Path $Root 'VERSION') -Raw).Trim() -ne $Version) {
        [IO.File]::WriteAllText((Join-Path $Root 'VERSION'), $Version)
        RunGit @('add', 'VERSION') | Out-Null
        RunGit @('commit', '-q', '-m', "Release $Version") | Out-Null
        $script:madeCommit = $true
    }
    RunGit @('tag', '-a', $Tag, '-m', "Release $Version") | Out-Null
    $script:madeTag = $true

    # --- 3. Build from the tag ------------------------------------------------------------------------------------
    Step "Building the installers from $Tag (in a clean folder)"
    New-Item -ItemType Directory $work | Out-Null
    RunGit @('archive', '--format=zip', '-o', "$work\source.zip", "refs/tags/$Tag") | Out-Null
    Expand-Archive "$work\source.zip" "$work\source"
    # (Its "already built" note is expected here: the tag's VERSION is this version.)
    # '' | gives the build no keyboard input, so it can't swallow your answer to the publish question.
    '' | & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$work\source\build.ps1" all $Version -Yes -WslDistro $WslDistro |
        Where-Object { $_ -notmatch 'was already built' }
    if ($LASTEXITCODE -ne 0) { throw 'Building the installers failed (see above).' }
    $files = @(Get-ChildItem "$work\source\dist" -File | Where-Object { $_.Name -like "*$Version*" })
    if ($files.Count -ne 2) { throw "Expected 2 installers, found: $($files.Name -join ', ')" }

    # --- 4. Privacy check ----------------------------------------------------------------------------------------
    Step 'Checking the installers for private information'
    $secrets = [ordered]@{}
    $envFile = Join-Path $Root '.env'
    if (Test-Path $envFile) {
        foreach ($key in 'DISCORD_TOKEN', 'GUILD_ID') {
            $v = ((Get-Content $envFile | Where-Object { $_ -match "^$key=" }) -replace "^$key=", '').Trim()
            if ($v.Length -ge 10) { $secrets["your $key (.env)"] = $v }
        }
    }
    $emails = @(& git config --global user.email 2>$null) + @(& git config --system user.email 2>$null) +
              @(& git log --all --format='%ae%n%ce' 2>$null) | Where-Object { $_ -and $_ -notlike '*@users.noreply.github.com' } | Sort-Object -Unique
    foreach ($e in $emails) { $secrets["email $e"] = $e }
    $secrets["your user folder"] = "\Users\$env:USERNAME\"
    $secrets["your user folder (/)"] = "/Users/$env:USERNAME/"
    $secrets["your PC's name"] = $env:COMPUTERNAME
    $secrets["OneDrive path"] = 'OneDrive\'
    # Anything else to keep out (e.g. a work email): one per line in private-strings.txt, which Git ignores.
    $extra = Join-Path $Root 'private-strings.txt'
    if (Test-Path $extra) { Get-Content $extra | Where-Object { $_.Trim() -and $_ -notmatch '^#' } | ForEach-Object { $secrets["private-strings.txt: $($_.Trim())"] = $_.Trim() } }

    $texts = @{}
    $exe = $files | Where-Object Extension -eq '.exe'
    $bytes = [IO.File]::ReadAllBytes($exe.FullName)
    $texts[$exe.Name] = [Text.Encoding]::ASCII.GetString($bytes) + [Text.Encoding]::Unicode.GetString($bytes)
    $deb = $files | Where-Object Extension -eq '.deb'
    # Unpack the .deb in WSL and print every file in it (a script file: PowerShell 5.1 mangles quotes passed inline).
    $unpack = "d=`$(mktemp -d)`ndpkg-deb -R `"`$1`" `"`$d`"`nfind `"`$d`" -type f -exec cat {} +`nrm -rf `"`$d`"`n"
    [IO.File]::WriteAllText("$work\unpack.sh", $unpack)
    $wsl = { param($p) (& wsl.exe -d $WslDistro -- wslpath -a ($p -replace '\\', '/') | Out-String).Trim() }
    $texts[$deb.Name] = (& wsl.exe -d $WslDistro -- bash (& $wsl "$work\unpack.sh") (& $wsl $deb.FullName) | Out-String)
    if ($texts[$deb.Name] -notmatch 'Package: discord-music-bot') { throw "Couldn't unpack $($deb.Name) to check it." }
    $leaks = foreach ($f in $texts.Keys) {
        foreach ($s in $secrets.Keys) { if ($texts[$f].IndexOf($secrets[$s], [StringComparison]::OrdinalIgnoreCase) -ge 0) { "$f contains $s" } }
    }
    if ($leaks) { throw "Private information found, nothing was published:`n  $($leaks -join "`n  ")" }
    Write-Host "Clean: checked for $($secrets.Count) private items ($((($secrets.Keys) -join ', ')))."

    # --- 5. Release notes, summary, confirmation -----------------------------------------------------------------
    $range = if ($lastTag) { "refs/tags/$lastTag..refs/tags/$Tag" } else { "refs/tags/$Tag" }
    $changes = @(RunGit @('log', '--format=%s', '--no-merges', $range) | Where-Object { $_ -notmatch '^Release \d' })
    $changeText = if ($changes) { ($changes | ForEach-Object { "- $_" }) -join "`n" } else { '- Maintenance release.' }
    $exeName = $exe.Name; $debName = $deb.Name
    $sums = ($files | ForEach-Object { "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)" }) -join "`n"
    $notes = @"
A Discord music bot with slash commands: plays YouTube and SoundCloud, plus Spotify and Apple Music links (matched to the best YouTube upload). Queue, loop (song or whole queue), shuffle, volume and saved playlists. Type ``/help`` in Discord for the full list.

## Download

| | File |
|---|---|
| **Windows** 10/11, 64-bit | ``$exeName`` |
| **Linux**: Debian, Ubuntu, Raspberry Pi OS (64-bit) | ``$debName`` |

You need your own Discord bot and its token: create one at https://discord.com/developers/applications (Bot -> Reset Token), and invite it to your server with the ``bot`` and ``applications.commands`` scopes and the Connect, Speak and Send Messages permissions.

### Windows

Run the setup and paste your bot token when it asks. It downloads its own Python, FFmpeg and Deno (a few minutes, needs internet) and runs the bot in the background from startup, restarting it if it crashes. If an older version is installed, it asks and replaces it (your token and playlists are kept). Manage it from **Start menu -> Discord Music Bot**.

The installer isn't code-signed, so Windows may show "Windows protected your PC": click **More info -> Run anyway**.

### Linux

``````
sudo apt install --no-install-recommends ./$debName
sudo discord-music-bot token
``````

It runs as the ``discord-music-bot`` systemd service: it starts on boot and restarts if it crashes. Run ``discord-music-bot`` for the other commands.

## What's new in $Version

$changeText

## Checksums (SHA-256)

``````
$sums
``````
"@
    [IO.File]::WriteAllText("$work\notes.md", $notes, (New-Object Text.UTF8Encoding $false))

    Write-Host "`n------------------------------------------------------------"
    Write-Host "Release $Tag to https://github.com/$repo (public)"
    $files | ForEach-Object { Write-Host ("  {0}  ({1:N0} KB)" -f $_.Name, ($_.Length / 1KB)) }
    Write-Host "What's new:"; Write-Host ($changeText -replace '(?m)^', '  ')
    Write-Host "------------------------------------------------------------"
    if ($DryRun) {
        $keep = Join-Path $env:TEMP "dmb-release-notes-$Version.md"
        Copy-Item "$work\notes.md" $keep -Force
        Write-Host "`nDry run: stopping here. The release notes it would publish: $keep" -ForegroundColor Yellow
        return
    }
    if ("$(Read-Host "`nPublish this release publicly on GitHub? [y/N]")".Trim() -notmatch '^(y|yes)$') {  # no answer = no
        Write-Host 'Not published.' -ForegroundColor Yellow
        return
    }

    # --- 6. Publish ------------------------------------------------------------------------------------------------
    Step 'Pushing the commit and tag'
    RunGit @('push', '-q', 'origin', 'refs/heads/main:refs/heads/main') | Out-Null
    $published = $true  # from here on, don't undo: the commit is on GitHub
    RunGit @('push', '-q', 'origin', "refs/tags/$Tag") | Out-Null

    Step 'Creating the GitHub release'
    & gh release create $Tag --repo $repo --verify-tag --title "Discord Music Bot $Version" --notes-file "$work\notes.md" --latest @($files.FullName)
    if ($LASTEXITCODE -ne 0) { throw "Creating the release failed. The tag $Tag is on GitHub; retry with: gh release create $Tag --verify-tag --notes-file <notes> <files>" }

    Step 'Checking the published downloads'
    foreach ($f in $files) {
        $url = "https://github.com/$repo/releases/download/$Tag/$($f.Name)"
        & curl.exe -fsSL --retry 3 -o "$work\check-$($f.Name)" $url
        $ok = ($LASTEXITCODE -eq 0) -and ((Get-FileHash "$work\check-$($f.Name)").Hash -eq (Get-FileHash $f.FullName).Hash)
        Write-Host "  $($f.Name): $(if ($ok) { 'OK' } else { 'MISMATCH - check the release page' })"
    }
    New-Item -ItemType Directory -Force (Join-Path $Root 'dist') | Out-Null
    $files | Copy-Item -Destination (Join-Path $Root 'dist') -Force
    Write-Host "`nReleased: https://github.com/$repo/releases/tag/$Tag  (installers also copied to dist)" -ForegroundColor Green
}
catch {
    Write-Host "`nError: $_" -ForegroundColor Red
    $failed = $true
}
finally {
    if (-not $published) { Undo }  # dry run, declined, failed or interrupted before publishing
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
if ($failed) { exit 1 }
