# gitScript.ps1 - build playlists, apply Channel Studio edits, then push.
#
# Logins live in accounts.csv next to this script (one row per run).
# accounts.csv is in .gitignore so it's never pushed; this script is safe to commit.
# Start it with gitScript.bat (double-click) or from PowerShell.

# ------------------------------------------------------------ settings
$Repo     = $PSScriptRoot                               # the folder this script is in
$Accounts = Join-Path $Repo "accounts.csv"
$Python   = "E:\Programming\.venv\Scripts\python.exe"   # your venv's python.exe ("python" works too)

# ------------------------------------------------------------ checks
Set-Location $Repo
$env:PYTHONIOENCODING = "utf-8"   # channel names with special characters print fine

if (-not (Test-Path $Accounts)) {
    Write-Host "Can't find $Accounts. Create it with these columns:" -ForegroundColor Red
    Write-Host "Name,Server,User,Pass,Args"
    exit 1
}

$Runs = @(Import-Csv $Accounts | Where-Object { $_.Name -and -not $_.Name.StartsWith("#") })
if ($Runs.Count -eq 0) {
    Write-Host "accounts.csv has no rows." -ForegroundColor Red
    exit 1
}
foreach ($r in $Runs) {
    if (-not $r.Server -or -not $r.User -or -not $r.Pass -or
        "$($r.Server) $($r.User) $($r.Pass)" -match "SERVER:PORT|USERNAME\d|PASSWORD\d") {
        Write-Host "Fill in the server, username and password for '$($r.Name)' in accounts.csv." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Path $Python)) {
    Write-Host "Couldn't find $Python - using 'python' from PATH instead." -ForegroundColor Yellow
    $Python = "python"
}

# git: use the one on PATH, or the copy that comes with GitHub Desktop
$Git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $Git) {
    $Git = Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop\app-*\resources\app\git\cmd\git.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Git) {
    Write-Host "Couldn't find git. Install Git for Windows or GitHub Desktop." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------ build playlists
$failed = @()
foreach ($r in $Runs) {
    Write-Host ""
    Write-Host "===== $($r.Name) =====" -ForegroundColor Cyan
    $extra = @("$($r.Args)".Trim() -split "\s+" | Where-Object { $_ })
    $runArgs = @((Join-Path $Repo "Xtream2m3u.py"), $r.Server, $r.User, $r.Pass) + $extra
    & $Python @runArgs
    if ($LASTEXITCODE -ne 0) { $failed += $r.Name }
}

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "These runs failed: $($failed -join ', '). Not pushing to GitHub." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------ create the default playlist from the untouched baseline
$BaselinePlaylist = Join-Path $Repo "strong.m3u"
$DefaultPlaylist = Join-Path $Repo "wes.m3u"
if (-not (Test-Path -LiteralPath $BaselinePlaylist -PathType Leaf)) {
    Write-Host "Can't create $DefaultPlaylist; generated baseline is missing: $BaselinePlaylist" -ForegroundColor Red
    exit 1
}

try {
    Copy-Item -LiteralPath $BaselinePlaylist -Destination $DefaultPlaylist -Force -ErrorAction Stop
}
catch {
    Write-Host "Couldn't create $DefaultPlaylist from $BaselinePlaylist`: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
Write-Host "Created wes.m3u from strong.m3u; strong.m3u remains the unedited baseline." -ForegroundColor Green

# ------------------------------------------------------------ apply Channel Studio edits and removals
$PlaylistEdits = Join-Path $Repo "edits.json"
if (Test-Path -LiteralPath $PlaylistEdits) {
    $Playlists = @(Get-ChildItem -LiteralPath $Repo -Filter "*.m3u" -File |
        Where-Object { $_.Name -ine "strong.m3u" } |
        Sort-Object Name)
    if ($Playlists.Count -eq 0) {
        Write-Host "Can't apply $PlaylistEdits because no M3U playlists were found." -ForegroundColor Red
        exit 1
    }

    foreach ($Playlist in $Playlists) {
        Write-Host "Applying edits to $($Playlist.Name)..." -ForegroundColor Cyan
        & $Python (Join-Path $Repo "apply_playlist_config.py") --playlist $Playlist.FullName --config $PlaylistEdits
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Applying edits.json to $($Playlist.Name) failed. Not publishing the playlists." -ForegroundColor Red
            exit 1
        }
    }
} else {
    Write-Host "No edits.json found; leaving the generated playlists unchanged." -ForegroundColor Yellow
}

# ------------------------------------------------------------ publish EPG source
$EPGSource = Join-Path $Repo "EPG.xml"
$EPGStrongLocal = Join-Path $Repo "EPGStrong.xml"
$EPGStrong = "\\ragnar\f\wwwroot\EPGStrong.xml"
if (-not (Test-Path -LiteralPath $EPGSource)) {
    Write-Host "Can't find EPG source: $EPGSource" -ForegroundColor Red
    exit 1
}

try {
    Copy-Item -LiteralPath $EPGSource -Destination $EPGStrongLocal -Force -ErrorAction Stop
}
catch {
    Write-Host "Couldn't copy EPG.xml to $EPGStrongLocal`: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

try {
    Copy-Item -LiteralPath $EPGSource -Destination $EPGStrong -Force -ErrorAction Stop
}
catch {
    Write-Host "Couldn't copy EPG.xml to $EPGStrong`: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host "Copied EPG.xml to $EPGStrongLocal and $EPGStrong." -ForegroundColor Green

# ------------------------------------------------------------ push to GitHub
Write-Host ""
Write-Host "===== GitHub =====" -ForegroundColor Cyan
& $Git -C $Repo add -A
if ($LASTEXITCODE -ne 0) { Write-Host "Staging changes failed." -ForegroundColor Red; exit 1 }
& $Git -C $Repo diff --cached --quiet
$HasChanges = ($LASTEXITCODE -ne 0)

if ($HasChanges) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    & $Git -C $Repo commit -m "Update playlists $stamp"
    if ($LASTEXITCODE -ne 0) { Write-Host "Commit failed." -ForegroundColor Red; exit 1 }
} else {
    Write-Host "No local changes to commit." -ForegroundColor Green
}

Write-Host "Pulling remote changes with rebase..." -ForegroundColor Cyan
& $Git -C $Repo pull --rebase
if ($LASTEXITCODE -ne 0) {
    Write-Host "Pull/rebase failed. Resolve any conflicts, then rerun the script." -ForegroundColor Red
    exit 1
}

& $Git -C $Repo push
if ($LASTEXITCODE -ne 0) { Write-Host "Push failed." -ForegroundColor Red; exit 1 }

if ($HasChanges) {
    Write-Host "Pushed to GitHub ($stamp)." -ForegroundColor Green
} else {
    Write-Host "Repository is up to date with GitHub." -ForegroundColor Green
}
