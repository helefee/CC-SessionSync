# CI only (throwaway runner): silent-install the built setup into a temp dir, check the installed exe, silent-uninstall.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$ver = ([regex]'__version__\s*=\s*"([^"]+)"').Match((Get-Content (Join-Path $root "cc_sessionsync\__init__.py") -Raw)).Groups[1].Value
$setup = Join-Path $root "dist\CCSessionSync-Setup-$ver.exe"
$want = (Get-Content "$setup.sha256" -Raw).Split(" ")[0].Trim()
if ((Get-FileHash $setup -Algorithm SHA256).Hash.ToLower() -ne $want) { throw "sha256 file does not match the setup" }
$dir = Join-Path $env:RUNNER_TEMP "ccss-installed"
$p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$dir`"" -Wait -PassThru
if ($p.ExitCode) { throw "setup exit $($p.ExitCode)" }
$exe = Join-Path $dir "CCSessionSync.exe"
if (-not (Test-Path $exe)) { throw "not installed: $exe" }
if (-not (Test-Path (Join-Path $dir "unins000.exe"))) { throw "no uninstaller" }
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "CC Session Sync.lnk"
if (-not (Test-Path $lnk)) { throw "no Start menu shortcut: $lnk" }
$env:CC_SESSIONSYNC_HOME = Join-Path $env:RUNNER_TEMP "ccss-home"
function Run([string[]]$a) {   # 跑装好的 exe，stdout / stderr 落到文件里再读（不靠控制台继承）
    $o = Join-Path $env:RUNNER_TEMP "ccss-out.txt"; $e = Join-Path $env:RUNNER_TEMP "ccss-err.txt"
    $p = Start-Process $exe -ArgumentList $a -Wait -PassThru -NoNewWindow -RedirectStandardOutput $o -RedirectStandardError $e
    $out = (Get-Content $o -Raw -Encoding utf8) + ""; $err = (Get-Content $e -Raw -Encoding utf8) + ""
    if ($p.ExitCode -and $p.ExitCode -ne 1) { throw "installed exe $($a -join ' ') -> exit $($p.ExitCode)`n$out`n$err" }
    return $out
}
Get-ChildItem $dir | Select-Object Name, Length | Format-Table -AutoSize | Out-String | Write-Host
$v = Run @("--version")
if ("$v" -notmatch [regex]::Escape($ver)) { throw "installed --version: [$v]" }
$sc = (Run @("selfcheck", "--offline", "--json")) | ConvertFrom-Json
$inst = $sc.items | Where-Object { $_.id -eq "install" }
Write-Host "installed selfcheck install: $($inst.status) $($inst.detail)"
if ($inst.status -ne "pass") { throw "installed integrity check failed" }
# 0.4.2 rollback: install the same setup again over it with a simulated failing health check -> the previous files come back
$before = (Get-FileHash $exe -Algorithm SHA256).Hash
$rbf = Join-Path $env:CC_SESSIONSYNC_HOME "update-rollback.json"
Remove-Item $rbf -ErrorAction SilentlyContinue
$p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$dir`"", "/FAILHEALTH=1" -Wait -PassThru
if ($p.ExitCode) { throw "setup (FAILHEALTH) exit $($p.ExitCode)" }
if (-not (Test-Path $exe)) { throw "rollback: exe missing after a failed health check" }
if (Test-Path "$dir.prev") { throw "rollback: $dir.prev left behind" }
if (-not (Test-Path $rbf)) { throw "rollback: no update-rollback.json" }
$rb = Get-Content $rbf -Raw -Encoding utf8 | ConvertFrom-Json
if ($rb.to -ne $ver -or -not $rb.from) { throw "rollback note: $(Get-Content $rbf -Raw)" }
if ((Get-FileHash $exe -Algorithm SHA256).Hash -ne $before) { throw "rollback: exe differs from the previous one" }
$v = Run @("--version"); if ("$v" -notmatch [regex]::Escape($ver)) { throw "after rollback --version: [$v]" }
$inst = ((Run @("selfcheck", "--offline", "--json")) | ConvertFrom-Json).items | Where-Object { $_.id -eq "install" }
if ($inst.status -ne "pass") { throw "after rollback integrity: $($inst.detail)" }
Write-Host "rollback after a failed health check OK: $(Get-Content $rbf -Raw)"
# normal upgrade (health passes): no .prev left, no new note
Remove-Item $rbf
$p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$dir`"" -Wait -PassThru
if ($p.ExitCode) { throw "setup (upgrade) exit $($p.ExitCode)" }
if ((Test-Path "$dir.prev") -or (Test-Path $rbf)) { throw "upgrade with passing health check left .prev or a rollback note" }
Write-Host "upgrade with passing health check OK"
$p = Start-Process (Join-Path $dir "unins000.exe") -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait -PassThru
Start-Sleep 3
if (Test-Path $exe) { throw "uninstall left the exe" }
if (Test-Path $lnk) { throw "uninstall left the shortcut" }
Write-Host "install / run / uninstall OK" -ForegroundColor Green
