#requires -version 5.1
# Assembles minimal runnable package next to this script: EXE + providers.json + backend.
# No test files, no stubs, no fixtures, no credentials are copied. Idempotent.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dist = Join-Path $root 'dist'
[void][IO.Directory]::CreateDirectory($dist)

# 1. EXE is rebuilt so package always matches sources.
& (Join-Path $root 'build-ui.ps1')
if ($LASTEXITCODE -ne 0) { throw 'UI build failed.' }

# 2. providers.json: the repo template is the single source of truth (a kept
#    stale copy once shipped old presets without the owner's edits).
Copy-Item -LiteralPath (Join-Path $root 'providers.json.template') (Join-Path $dist 'providers.json') -Force

# 3. Backend: worker + helpers only. upstream install.ps1 verified by checksum at runtime.
[void][IO.Directory]::CreateDirectory((Join-Path $dist 'backend'))
foreach ($name in @('worker.ps1','streaming.ps1','checkpoint.ps1','protect.ps1','configure.py','provider.py','extras.py','telegram.py','fallbacks.py')) {
    Copy-Item -LiteralPath (Join-Path $root "backend\$name") (Join-Path $dist "backend\$name") -Force
}
[void][IO.Directory]::CreateDirectory((Join-Path $dist 'backend\upstream'))
foreach ($name in @('install.ps1','install.sha256','commit.txt')) {
    Copy-Item -LiteralPath (Join-Path $root "backend\upstream\$name") (Join-Path $dist "backend\upstream\$name")
}
# cli-config.yaml.example is the pristine template configure.py may replace on fresh installs.
Copy-Item -LiteralPath (Join-Path $root 'backend\upstream\cli-config.yaml.example') (Join-Path $dist 'backend\upstream\cli-config.yaml.example')

# 3a. Out-of-box assets: worker reads them next to itself ($PSScriptRoot\assets).
$assetsDist = Join-Path $dist 'backend\assets'
if (Test-Path -LiteralPath $assetsDist) { Remove-Item -LiteralPath $assetsDist -Recurse -Force }
[void][IO.Directory]::CreateDirectory($assetsDist)
Copy-Item -LiteralPath (Join-Path $root 'assets\SOUL.md') (Join-Path $assetsDist 'SOUL.md') -Force
Copy-Item -LiteralPath (Join-Path $root 'assets\skills') (Join-Path $assetsDist 'skills') -Recurse -Force
# Marketplaces MCP launcher + Wildberries server: the .py files only (never a __pycache__ left by tests).
[void][IO.Directory]::CreateDirectory((Join-Path $assetsDist 'marketplaces'))
foreach ($name in @('direct_launcher.py','direct_common.py','direct_proxy.py','wb_browser_mcp.py')) {
    Copy-Item -LiteralPath (Join-Path $root "assets\marketplaces\$name") (Join-Path $assetsDist "marketplaces\$name") -Force
}

# 3b. Double-click entry point and human instructions, rebuilt every package.
$launcher = "@echo off`r`nstart `"`" `"%~dp0HermesSetup.exe`"`r`n"
[IO.File]::WriteAllText((Join-Path $dist 'Как установить.cmd'), $launcher, (New-Object Text.UTF8Encoding($false)))
Copy-Item -LiteralPath (Join-Path $root 'user-readme.txt') (Join-Path $dist 'Прочти меня.txt') -Force

# 4. Guarantees: nothing stub/test/secret-like ships.
$suspects = @(Get-ChildItem -LiteralPath $dist -Recurse -File | Where-Object {
    $_.Name -match '(^test-|^check_|stub|mock|fixture)' -or $_.Extension -in '.log','.pyc','.key','.env','.session','.session-journal' })
if ($suspects.Count -gt 0) { throw ('Package contains forbidden files: ' + (($suspects | ForEach-Object FullName) -join ', ')) }

# 4a. Content scan: a real credential inside an otherwise innocent text file
# (a note, a skill, a json) passes the name/extension filter above. Patterns
# require real-looking payloads, so documented placeholders such as
# GWAR-XXXX or apx_live_... do not trip the build.
$secretPatterns = @(
    'SESSION_STRING["'']?\s*[=:]\s*["'']?[A-Za-z0-9+/=_-]{20,}',
    'TELEGRAM_BOT_TOKEN["'']?\s*[=:]\s*["'']?\d{6,}:[A-Za-z0-9_-]{20,}',
    '(?<![A-Za-z0-9_-])\d{7,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])',
    'tvly-[A-Za-z0-9_-]{16,}',
    '(?<![A-Za-z0-9_-])sk-(or-v1-|proj-|ant-)?[A-Za-z0-9_-]{24,}',
    'GWAR-(?!X+\b)[A-Za-z0-9]{8,}',
    'apx_live_[A-Za-z0-9]{16,}',
    'dahl_[A-Za-z0-9]{16,}'
)
# Scan everything except known binaries: a skill can bring .js/.sh/.toml/no-extension
# files, and an allow-list silently skips them (security review 2026-09-22).
$binaryExt = '.exe','.dll','.pdb','.png','.jpg','.jpeg','.gif','.ico','.zip','.7z','.woff','.woff2'
$leaks = @()
foreach ($file in (Get-ChildItem -LiteralPath $dist -Recurse -File | Where-Object { $_.Extension -notin $binaryExt })) {
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($pattern in $secretPatterns) {
        if ($text -match $pattern) { $leaks += ($file.FullName.Replace($dist, '') + '  [' + $pattern.Substring(0, [Math]::Min(18, $pattern.Length)) + '...]') }
    }
}
if ($leaks.Count -gt 0) { throw ('Package contains credential-like content: ' + ($leaks -join '; ')) }

# 4b. Template/pin sync: configure.py only replaces a config that is byte-identical
# to the shipped cli-config.yaml.example. If the pinned Hermes commit moves and the
# template is not refreshed, every subscriber gets "existing model settings found".
$pin = (Get-Content -LiteralPath (Join-Path $root 'backend\upstream\commit.txt') -Raw).Trim()
$shipped = Join-Path $root 'backend\upstream\cli-config.yaml.example'
try {
    $remote = Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri "https://raw.githubusercontent.com/NousResearch/hermes-agent/$pin/cli-config.yaml.example"
    $remoteHash = [BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash($remote.RawContentStream.ToArray())).Replace('-', '')
    $localHash = (Get-FileHash -LiteralPath $shipped -Algorithm SHA256).Hash
    if ($remoteHash -ne $localHash) { throw "cli-config.yaml.example does not match pinned commit $pin. Refresh backend\upstream\cli-config.yaml.example before packaging." }
    Write-Output "template/pin sync: OK ($pin)"
} catch [System.Net.WebException] {
    # A wrong pin answers 404 - that is a broken package, not a network hiccup.
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    if ($status -eq 404) { throw "Pinned commit $pin has no cli-config.yaml.example (HTTP 404): wrong pin in backend\upstream\commit.txt." }
    Write-Warning "template/pin sync NOT verified (network): $($_.Exception.Message)"
}
Get-ChildItem -LiteralPath $dist -Recurse -File | ForEach-Object { $_.IsReadOnly = $false }

Write-Output '--- dist files ---'
Get-ChildItem -LiteralPath $dist -Recurse -File | ForEach-Object { $_.FullName.Replace($dist,'') + '  ' + $_.Length }
Write-Output '--- SHA256 ---'
Get-ChildItem -LiteralPath $dist -Recurse -File | Get-FileHash -Algorithm SHA256 | Format-Table Hash,Path -AutoSize
