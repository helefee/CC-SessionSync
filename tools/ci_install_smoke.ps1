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
$p = Start-Process (Join-Path $dir "unins000.exe") -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait -PassThru
Start-Sleep 3
if (Test-Path $exe) { throw "uninstall left the exe" }
if (Test-Path $lnk) { throw "uninstall left the shortcut" }
Write-Host "install / run / uninstall OK" -ForegroundColor Green
