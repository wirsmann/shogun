$Repo     = $PSScriptRoot                               # the folder this script is in

# ------------------------------------------------------------ checks
Set-Location $Repo

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

$Branch = & $Git -C $Repo symbolic-ref --quiet --short HEAD 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "This repository is in detached HEAD state; nothing has been staged or committed." -ForegroundColor Red
    Write-Host "Check out the branch you use for publishing (usually main), then rerun this script." -ForegroundColor Yellow
    Write-Host "If a previous run committed changes while detached, preserve that commit before switching branches." -ForegroundColor Yellow
    exit 1
}

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