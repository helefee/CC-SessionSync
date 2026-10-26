# Build the Windows installer version of CC Session Sync (structure ported from CC Msg's tools\build_desktop.ps1):
#   dist\CCSessionSync\CCSessionSync.exe        Nuitka standalone (onedir), Windows GUI, attaches to a console when run from one
#                                               (same exe = desktop window + every CLI subcommand + the apiKeyHelper `key get`)
#   dist\CCSessionSync\files.sha256             manifest of every program file (自检 checks it)
#   dist\CCSessionSync-Setup-<ver>.exe (+ .sha256)   Inno Setup installer, per-user (%LOCALAPPDATA%\Programs\CCSessionSync), no admin
# No .py sources end up in dist; Python is not needed on the target machine.
# Usage:  pwsh tools\build_desktop.ps1 [-NoTest] [-NoInstaller]
# Needs:  64-bit Python 3.11 (only to build), Inno Setup 6.5+ (ISCC.exe on PATH / Program Files / $env:ISCC).
param(
    [switch]$NoTest,
    [switch]$NoInstaller
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
$distRoot = Join-Path $root "dist"
$out = Join-Path $distRoot "CCSessionSync"
New-Item -ItemType Directory -Force $build, $distRoot | Out-Null
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function Native { $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"; try { & $args[0] @($args | Select-Object -Skip 1) 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $eap } }

$ver = ([regex]'__version__\s*=\s*"([^"]+)"').Match((Get-Content (Join-Path $root "cc_sessionsync\__init__.py") -Raw)).Groups[1].Value
if (-not $ver) { throw "no __version__ in cc_sessionsync\__init__.py" }
$numVer = ([regex]'^\d+(\.\d+){0,3}').Match($ver).Value
Write-Host "CC Session Sync $ver"

Step "build venv"
$venv = Join-Path $build "venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    $sysPy = $null
    $probe = "import sys; print(sys.executable if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else '')"
    foreach ($cmd in @("py -3.11", "python")) {
        try {
            $parts = $cmd.Split(" ")
            $exe = if ($parts.Length -gt 1) { & $parts[0] $parts[1] -c $probe 2>$null } else { & $parts[0] -c $probe 2>$null }
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { $sysPy = $exe.Trim(); break }
        } catch {}
    }
    if (-not $sysPy) { throw "Need a 64-bit Python 3.11 (py -3.11 or python) to build." }
    & $sysPy -m venv $venv; Check "venv"
}
Native $vpy -m pip install --disable-pip-version-check -q --upgrade pip wheel; Check "pip upgrade"
Native $vpy -m pip install --disable-pip-version-check -q --upgrade -r (Join-Path $root "tools\requirements-build.txt"); Check "pip install"

$cc = @()
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$hasMsvc = (Test-Path $vswhere) -and (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
if ($hasMsvc) { $cc = @("--msvc=latest"); Write-Host "    compiler: MSVC" } else { $cc = @("--mingw64"); Write-Host "    compiler: MinGW64 (Nuitka downloads it)" }
Push-Location $root
try {
    $env:PYTHONPATH = $root
    Step "nuitka CCSessionSync.exe"
    Native $vpy -m nuitka --standalone --assume-yes-for-downloads --remove-output --python-flag=no_docstrings `
        "--company-name=helefee" "--product-name=CC Session Sync" "--file-version=$numVer" "--product-version=$numVer" `
        "--copyright=MIT" "--windows-icon-from-ico=cc_sessionsync\icon.ico" @cc `
        "--output-dir=build\nuitka-app" "--output-filename=CCSessionSync.exe" `
        "--windows-console-mode=attach" "--file-description=CC Session Sync" `
        "--include-package=cc_sessionsync" "--include-package-data=cc_sessionsync" "--include-package=zstandard" "--include-package=yaml" `
        "--nofollow-import-to=unittest,pydoc,test" `
        "tools\nuitka\CCSessionSync.py"
    Check "nuitka"
} finally { Pop-Location; Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }

Step "assemble $out"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
Copy-Item -Recurse (Join-Path $build "nuitka-app\CCSessionSync.dist") $out
foreach ($f in @("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md")) { Copy-Item (Join-Path $root $f) $out }
Copy-Item -Recurse (Join-Path $root "licenses") (Join-Path $out "licenses")
$py = @(Get-ChildItem $out -Recurse -File -Include *.py, *.pyw, *.pyc)
if ($py.Count) { $py | ForEach-Object { Write-Host "    $($_.FullName)" }; throw "python sources found in $out" }
foreach ($need in @("cc_sessionsync\index.html", "cc_sessionsync\icon.ico", "cc_sessionsync\icon.png", "cc_sessionsync\tray-badge.ico", "cc_sessionsync\usage_prices.json", "cc_sessionsync\codex_prompt.md", "cc_sessionsync\fonts\inter-regular.woff2")) {
    if (-not (Test-Path (Join-Path $out $need))) { throw "missing in dist: $need" }
}
# manifest for 自检 (install integrity)
$lines = Get-ChildItem $out -Recurse -File | Where-Object { $_.Name -ne "files.sha256" } | Sort-Object FullName | ForEach-Object {
    $rel = $_.FullName.Substring($out.Length + 1)
    "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $rel"
}
[IO.File]::WriteAllLines((Join-Path $out "files.sha256"), $lines, (New-Object Text.UTF8Encoding $false))
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("    {0:N1} MB, {1} files, no .py files" -f ($size / 1MB), $lines.Count)

if (-not $NoTest) {
    Step "smoke test (hidden, temp data dirs, port 8799, no Python on PATH)"
    $exe = Join-Path $out "CCSessionSync.exe"
    $t = Join-Path $env:TEMP ("ccss-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force $t | Out-Null
    $saved = @{}
    foreach ($k in @("CC_SESSIONSYNC_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CC_SESSIONSYNC_NO_SELFCHECK", "CC_SESSIONSYNC_INSTANCE", "PATH")) { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
    $env:CC_SESSIONSYNC_HOME = "$t\ss"; $env:CLAUDE_CONFIG_DIR = "$t\claude"; $env:CODEX_HOME = "$t\codex"; $env:CC_SESSIONSYNC_NO_SELFCHECK = "1"
    $env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and -not (Test-Path (Join-Path $_ "python.exe")) -and -not (Test-Path (Join-Path $_ "py.exe")) }) -join ';'
    try {
        $v = Native $exe --version; if ($LASTEXITCODE -or ("$v" -notmatch [regex]::Escape($ver))) { throw "--version: $v" }
        Native $exe status | Out-Null; Check "status"
        Native $exe codex list --days 0 | Out-Null; Check "codex list"
        Native $exe sessions list | Out-Null; Check "sessions list"
        $sc = Native $exe selfcheck --offline --json
        $scj = ($sc -join "`n") | ConvertFrom-Json
        $inst = $scj.items | Where-Object { $_.id -eq "install" }
        Write-Host "    selfcheck: pass $($scj.counts.pass) / warn $($scj.counts.warn) / fail $($scj.counts.fail); install: $($inst.detail)"
        if ($inst.status -ne "pass") { throw "selfcheck install integrity: $($inst.detail)" }
        $p = Start-Process -FilePath $exe -ArgumentList "--port", "8799", "--instance", "SMOKE", "--minimized" -PassThru -WindowStyle Hidden
        $ok = $false
        for ($i = 0; $i -lt 60; $i++) {
            try { $h = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/ping -TimeoutSec 2; if ($h.Content -match "cc-sessionsync") { $ok = $true; break } } catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $ok) { throw "desktop smoke: server on 8799 did not come up" }
        $page = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/ -TimeoutSec 10).Content
        $tok = ([regex]'const TOKEN = "([^"]+)"').Match($page).Groups[1].Value
        $s = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/status -Headers @{"x-token" = $tok} -TimeoutSec 20
        if ($s.Content -notmatch '"version"') { throw "desktop smoke: api/status" }
        try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/status -TimeoutSec 5 | Out-Null; throw "api without token was not refused" } catch { if ("$_" -match "not refused") { throw } }
        Native $exe --quit --instance SMOKE | Out-Null
        if (-not $p.WaitForExit(20000)) { Stop-Process -Id $p.Id -Force; throw "desktop smoke: --quit did not stop it" }
        Write-Host "    desktop window (hidden) + tray + server + --quit OK"
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
        Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
    }
}

if (-not $NoInstaller) {
    Step "Inno Setup"
    $iscc = $env:ISCC
    if (-not $iscc) { $c = Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($c) { $iscc = $c.Source } }
    foreach ($d in @("${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6", "$env:LOCALAPPDATA\Programs\Inno Setup 6", "$env:ProgramFiles\Inno Setup 7", "$env:LOCALAPPDATA\Programs\Inno Setup 7")) {
        if (-not $iscc -and (Test-Path "$d\ISCC.exe")) { $iscc = "$d\ISCC.exe" }
    }
    if (-not $iscc) { throw "ISCC.exe (Inno Setup 6.5+) not found; install it or set `$env:ISCC" }
    Native $iscc /Q "/DAppVersion=$ver" "/DSourceDir=$out" "/DOutputDir=$distRoot" (Join-Path $root "installer\CCSessionSync.iss")
    Check "ISCC"
    $setup = Join-Path $distRoot "CCSessionSync-Setup-$ver.exe"
    $hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
    [IO.File]::WriteAllText("$setup.sha256", "$hash  CCSessionSync-Setup-$ver.exe`n", (New-Object Text.UTF8Encoding $false))
    Write-Host ("    {0} ({1:N1} MB)  sha256 {2}" -f $setup, ((Get-Item $setup).Length / 1MB), $hash) -ForegroundColor Green
}
Write-Host "done: $out\CCSessionSync.exe" -ForegroundColor Green
