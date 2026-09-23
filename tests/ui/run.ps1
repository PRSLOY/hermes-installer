$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$sources = @(Get-ChildItem -LiteralPath (Join-Path $root 'ui') -Filter '*.cs' | ForEach-Object FullName)
& $csc /nologo /main:UnitTests /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "/out:$PSScriptRoot\ProtocolTests.exe" @sources $PSScriptRoot\ProtocolTests.cs $PSScriptRoot\E2eTests.cs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$PSScriptRoot\ProtocolTests.exe"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output '--- e2e (mock backend, SIMULATION ONLY) ---'
& "$PSScriptRoot\ProtocolTests.exe" e2e
exit $LASTEXITCODE
