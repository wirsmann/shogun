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