# 用 Nuitka 编出单文件 dist\cc-sessionsync.exe，再跑一遍冒烟测试。需要 Python 3.11 x64。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m pip install --upgrade -r tools\requirements-build.txt
$v = ([regex]'__version__\s*=\s*"([^"]+)"').Match((Get-Content cc_sessionsync\__init__.py -Raw)).Groups[1].Value
$nv = "$v.0"; while (($nv.Split('.')).Count -lt 4) { $nv += ".0" }
python -m nuitka --mode=onefile --assume-yes-for-downloads --output-dir=build --output-filename=cc-sessionsync.exe `
  --include-package=cc_sessionsync --include-package-data=cc_sessionsync `
  --windows-console-mode=force --company-name=helefee --product-name="CC Session Sync" `
  --file-version=$nv --product-version=$nv --file-description="CC Session Sync" `
  --python-flag=no_site --nofollow-import-to=tkinter,unittest,pydoc,test `
  tools\exe_entry.py
if ($LASTEXITCODE) { throw "nuitka failed" }
New-Item -ItemType Directory -Force dist | Out-Null
$exe = "dist\cc-sessionsync-$v-windows-x64.exe"
Copy-Item build\cc-sessionsync.exe $exe -Force
# 冒烟：临时目录里跑 --version / status / codex list / diag
$t = Join-Path $env:RUNNER_TEMP "exe-smoke"; if (-not $env:RUNNER_TEMP) { $t = Join-Path $env:TEMP "ccss-exe-smoke" }
New-Item -ItemType Directory -Force $t | Out-Null
$env:CC_SESSIONSYNC_HOME = "$t\ss"; $env:CLAUDE_CONFIG_DIR = "$t\claude"; $env:CODEX_HOME = "$t\codex"
$out = & $exe --version; if ($LASTEXITCODE -or ($out -notmatch [regex]::Escape($v))) { throw "version smoke failed: $out" }
& $exe status; if ($LASTEXITCODE) { throw "status smoke failed" }
& $exe codex list --days 0; if ($LASTEXITCODE) { throw "codex list smoke failed" }
& $exe diag | Out-Null; if ($LASTEXITCODE) { throw "diag smoke failed" }
$p = Start-Process -FilePath $exe -ArgumentList "ui","--port","8799","--no-browser" -PassThru -WindowStyle Hidden
try {
  $ok = $false
  for ($i = 0; $i -lt 40; $i++) { try { $h = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/ -TimeoutSec 2; if ($h.Content -match "CC Session Sync") { $ok = $true; break } } catch { Start-Sleep -Milliseconds 500 } }
  if (-not $ok) { throw "ui smoke failed" }
  $s = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/api/status -TimeoutSec 20
  if ($s.Content -notmatch '"version"') { throw "api smoke failed" }
} finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
$h = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$h  $(Split-Path $exe -Leaf)" | Out-File -Encoding ascii "$exe.sha256"
Write-Host "built $exe $h"
