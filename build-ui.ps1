$ErrorActionPreference = 'Stop'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework 4 compiler missing. Build on Windows with .NET Framework 4.8.' }
$out = Join-Path $PSScriptRoot 'dist\HermesSetup.exe'
[void][IO.Directory]::CreateDirectory((Split-Path $out -Parent))
$sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'ui') -Filter '*.cs' | ForEach-Object FullName)
& $csc /nologo /target:winexe /platform:x64 /optimize+ /utf8output /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "/out:$out" $sources
if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed.' }
Get-FileHash -LiteralPath $out -Algorithm SHA256 | Format-List
