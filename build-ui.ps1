$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework 4 compiler missing. Build on Windows with .NET Framework 4.8.' }
$out = Join-Path $PSScriptRoot 'dist\HermesSetup.exe'
[void][IO.Directory]::CreateDirectory((Split-Path $out -Parent))
$sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'ui') -Filter '*.cs' | ForEach-Object FullName)
# The release-pinned artifact hashes are embedded in the EXE (issue #15), so a blocked
# GitHub never stops verification and the manifest cannot be swapped apart from the EXE.
$manifest = Join-Path $PSScriptRoot 'backend\upstream\artifacts.sha256.json'
if (-not (Test-Path -LiteralPath $manifest)) { throw 'backend/upstream/artifacts.sha256.json missing. Run tools/gen-artifacts-manifest.ps1 before building.' }
& $csc /nologo /target:winexe /platform:x64 /optimize+ /utf8output /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "/resource:$manifest,HermesSetup.artifacts.sha256.json" "/out:$out" $sources
if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed.' }
Get-FileHash -LiteralPath $out -Algorithm SHA256 | Format-List
