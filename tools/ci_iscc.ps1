# CI: find (or choco-install) Inno Setup and put ISCC=<path> into $GITHUB_ENV.
$c = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($c) { $c = $c.Source } else {
    foreach ($d in "${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6", "$env:ProgramFiles\Inno Setup 7") { if (Test-Path "$d\ISCC.exe") { $c = "$d\ISCC.exe" } }
    if (-not $c) {
        choco install innosetup -y --no-progress
        foreach ($d in "${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6", "$env:ProgramFiles\Inno Setup 7") { if (Test-Path "$d\ISCC.exe") { $c = "$d\ISCC.exe" } }
    }
}
if (-not $c) { throw "no ISCC.exe" }
Write-Host "ISCC: $c"
"ISCC=$c" | Out-File -Append -Encoding utf8 $env:GITHUB_ENV
