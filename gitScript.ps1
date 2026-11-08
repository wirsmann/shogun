# gitScript.ps1 - build all the playlists, then push the changes to GitHub.
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

# ------------------------------------------------------------ push to GitHub
Write-Host ""
Write-Host "===== GitHub =====" -ForegroundColor Cyan
& $Git -C $Repo add -A
& $Git -C $Repo diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "Nothing changed - nothing to push." -ForegroundColor Green
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
& $Git -C $Repo commit -m "Update playlists $stamp"
if ($LASTEXITCODE -ne 0) { Write-Host "Commit failed." -ForegroundColor Red; exit 1 }

& $Git -C $Repo push
if ($LASTEXITCODE -ne 0) { Write-Host "Push failed." -ForegroundColor Red; exit 1 }

Write-Host "Pushed to GitHub ($stamp)." -ForegroundColor Green
