$ErrorActionPreference='Stop'
$root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$out=Join-Path $root 'qa\ui-layout'
[void][IO.Directory]::CreateDirectory($out)
Copy-Item (Join-Path $root 'providers.json.template') (Join-Path $out 'providers.json') -Force
$csc=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$sources=@(Get-ChildItem (Join-Path $root 'ui') -Filter '*.cs' | ForEach-Object FullName)
& $csc /nologo /main:LayoutTests /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll "/out:$out\LayoutTests.exe" @sources "$PSScriptRoot\LayoutTests.cs"
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
& "$out\LayoutTests.exe" $out
exit $LASTEXITCODE
