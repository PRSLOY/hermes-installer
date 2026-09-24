#requires -version 5.1
<#
Generates backend/upstream/artifacts.sha256.json -- the release-pinned artifact
hashes that are embedded into HermesSetup.exe (see issue #15).

Run at release time, after bumping backend/upstream/commit.txt and the pinned
versions below. Each artifact is downloaded once (cached under %TEMP%), hashed,
and recorded. Nothing here runs at install time: install.ps1 reads the manifest
from the environment, and the manifest is embedded in the EXE at build time, so
a blocked GitHub does not stop verification.

Usage:
    powershell -ExecutionPolicy Bypass -File .\tools\gen-artifacts-manifest.ps1
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = Split-Path $PSScriptRoot -Parent
$cache = Join-Path ([IO.Path]::GetTempPath()) 'hermes-artifacts-cache'
[void][IO.Directory]::CreateDirectory($cache)
$outPath = Join-Path $root 'backend\upstream\artifacts.sha256.json'

# --- pinned versions (bump together with commit.txt) ------------------------
$gitTag = 'v2.54.0.windows.1'
$gitVer = '2.54.0'
$nodeMajor = '22'
$commit = (Get-Content -LiteralPath (Join-Path $root 'backend\upstream\commit.txt') -Raw).Trim()

function Get-Cached([string]$Url, [string]$Name) {
    $dest = Join-Path $cache $Name
    if (Test-Path -LiteralPath $dest) { Write-Host "cache hit  $Name"; return $dest }
    Write-Host "downloading $Name ..."
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 900 -Uri $Url -OutFile $dest
    return $dest
}
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
# Deterministic content hash of a source archive: sorted normalized entry names
# (root directory stripped, forward slashes), per entry "<rel>`0<hex sha256 of
# decompressed bytes>", joined with "`n", then SHA-256 of the whole thing.
# Independent of the archive's compression settings, so it stays valid when
# GitHub regenerates the zip for the same commit (issue #15).
function Get-ArchiveContentSha256([string]$ZipPath) {
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $items = @()
        foreach ($e in $zip.Entries) {
            $name = $e.FullName.Replace('\', '/')
            if ($name.EndsWith('/')) { continue }
            $slash = $name.IndexOf('/')
            if ($slash -lt 0) { continue }
            $rel = $name.Substring($slash + 1)
            if ($rel -eq '') { continue }
            $items += [pscustomobject]@{ Rel = $rel; Entry = $e }
        }
        $rels = @($items | ForEach-Object { $_.Rel })
        [Array]::Sort($rels, [System.StringComparer]::Ordinal)
        $byRel = @{}
        foreach ($it in $items) { $byRel[$it.Rel] = $it.Entry }
        $sha = [Security.Cryptography.SHA256]::Create()
        $sb = New-Object System.Text.StringBuilder
        try {
            foreach ($rel in $rels) {
                $stream = $byRel[$rel].Open()
                try { $hex = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
                finally { $stream.Dispose() }
                [void]$sb.Append($rel).Append([char]0).Append($hex).Append("`n")
            }
        } finally { $sha.Dispose() }
        $sha2 = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha2.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))).Replace('-', '').ToLowerInvariant() }
        finally { $sha2.Dispose() }
    } finally { $zip.Dispose() }
}

# Resolve the exact current Node.js patch for the pinned major.
$nodeIndex = Get-Cached "https://nodejs.org/dist/latest-v${nodeMajor}.x/" 'node-index.html'
$nodeVersion = ([regex]"node-v(\d+\.\d+\.\d+)-win-x64\.zip").Match((Get-Content $nodeIndex -Raw)).Groups[1].Value
if (-not $nodeVersion) { throw 'Could not resolve the current Node.js version from the index.' }
Write-Host "pinned Node.js: v$nodeVersion"

$artifacts = @()

# --- PortableGit (Git for Windows), per architecture ------------------------
$gitMirrors = @(
    'https://registry.npmmirror.com/-/binary/git-for-windows/',
    'https://mirrors.huaweicloud.com/git-for-windows/'
)
foreach ($g in @(
    @{ arch = 'x64';   asset = "PortableGit-$gitVer-64-bit.7z.exe" },
    @{ arch = 'arm64'; asset = "PortableGit-$gitVer-arm64.7z.exe" },
    @{ arch = 'x86';   asset = "MinGit-$gitVer-32-bit.zip" }
)) {
    $canonical = "https://github.com/git-for-windows/git/releases/download/$gitTag/$($g.asset)"
    $file = Get-Cached $canonical $g.asset
    $sources = @($canonical) + @($gitMirrors | ForEach-Object { "$_$gitTag/$($g.asset)" })
    $artifacts += [ordered]@{
        id = 'portablegit'; arch = $g.arch; version = $gitVer
        sha256 = Get-Sha256 $file; sources = $sources
    }
}

# --- Node.js, per architecture ----------------------------------------------
$nodeMirrors = @('https://registry.npmmirror.com/-/binary/node/')
foreach ($arch in @('x64', 'arm64')) {
    $zip = "node-v$nodeVersion-win-$arch.zip"
    $canonical = "https://nodejs.org/dist/v$nodeVersion/$zip"
    $file = Get-Cached $canonical $zip
    $sources = @($canonical) + @($nodeMirrors | ForEach-Object { "$($_)v$nodeVersion/$zip" })
    $artifacts += [ordered]@{
        id = 'node'; arch = $arch; version = "v$nodeVersion"
        sha256 = Get-Sha256 $file; sources = $sources
    }
}

# --- Hermes source archive (content hash, not the zip bytes) ----------------
$hermesProxies = @('https://ghproxy.net/', 'https://gh-proxy.com/')
$hermesZip = "hermes-agent-$commit.zip"
$hermesCanonical = "https://github.com/NousResearch/hermes-agent/archive/$commit.zip"
$hermesFile = Get-Cached $hermesCanonical $hermesZip
$artifacts += [ordered]@{
    id = 'hermes-source'; commit = $commit
    content_sha256 = Get-ArchiveContentSha256 $hermesFile
    sources = @($hermesCanonical) + @($hermesProxies | ForEach-Object { "$_$hermesCanonical" })
}

# --- vendored install.ps1 ---------------------------------------------------
$artifacts += [ordered]@{
    id = 'install-ps1'
    sha256 = Get-Sha256 (Join-Path $root 'backend\upstream\install.ps1')
}

$manifest = [ordered]@{ schema = 1; artifacts = $artifacts }
$json = $manifest | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($outPath, $json, (New-Object Text.UTF8Encoding($false)))
Write-Host "wrote $outPath"
Get-Content -LiteralPath $outPath
