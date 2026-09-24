# Installer-owned journal. No deletion/repair of partial trees, no secrets.
# Captured at load time: inside a background job $PSScriptRoot follows the caller,
# so the stage-hash job must be given this absolute path explicitly.
$script:CheckpointDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $PSCommandPath }
function Assert-SafePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[A-Za-z]:\\' -or $full.Substring(3) -match '[:*?]' -or $full -match '[ .](\\|$)') { Fail 'CONFIG' 'Небезопасный путь установки.' }
    $part = $full
    while ($part) {
        $item = Get-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { Fail 'CONFIG' 'Ссылки и junction в путях установки запрещены.' }
        $part = [IO.Path]::GetDirectoryName($part)
    }
    return $full.TrimEnd('\')
}
function Journal-Path($HomeDir) { return ($HomeDir + '.subscriber-checkpoint.json') }
function Journal-Files($HomeDir, $Repo) {
    $name = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {'win-arm64-unpacked'} else {'win-unpacked'}
    return @((Join-Path $HomeDir 'config.yaml'),(Join-Path $HomeDir '.env'),(Join-Path $HomeDir 'bin\hermes.exe'),
        (Join-Path $Repo 'venv\Scripts\python.exe'),(Join-Path $Repo '.git\HEAD'),(Join-Path $Repo 'cli-config.yaml.example'),
        (Join-Path $Repo "apps\desktop\release\$name\Hermes.exe"),(Join-Path $Repo "apps\desktop\release\$name\resources\app.asar"),
        (Join-Path $Repo "apps\desktop\release\$name\icudtl.dat"))
}
function Journal-Fingerprints($HomeDir, $Repo) {
    $out=@()
    foreach ($f in (Journal-Files $HomeDir $Repo)) {
        $null=Assert-SafePath $f
        if (Test-Path -LiteralPath $f) {
            if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Fail 'CONFIG' 'Ожидался обычный файл установки.' }
            $out += (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash
        } else { $out += 'absent' }
    }
    return ,$out
}
function Write-Journal($State, [switch]$New) {
    $path=Journal-Path $State.home; $null=Assert-SafePath $path
    # DPAPI binds the journal to this Windows user and detects modified payloads.
    Add-Type -AssemblyName System.Security
    $bytes=[Text.Encoding]::UTF8.GetBytes(($State|ConvertTo-Json -Compress -Depth 5))
    $sealed=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    $tmp=$path+'.'+[Guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllBytes($tmp,$sealed)
    try {
        if ($New) { [IO.File]::Move($tmp,$path) } else { [IO.File]::Replace($tmp,$path,[NullString]::Value) }
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}
function Read-Journal($HomeDir, $Repo, $Pin, $Sid) {
    $null=Assert-SafePath $HomeDir; $null=Assert-SafePath $Repo
    $path=Journal-Path $HomeDir; $null=Assert-SafePath $path
    try {
        if ((Get-Item -LiteralPath $path -Force).Length -gt 32768) { throw 'size' }
        Add-Type -AssemblyName System.Security
        $bytes=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
        $s=[Text.Encoding]::UTF8.GetString($bytes)|ConvertFrom-Json
        $fields=@($s.PSObject.Properties.Name|Sort-Object)
        if (($fields -join ',') -cne 'fingerprints,home,owner,phase,repo,revision,schema,sid') { throw 'schema' }
        if ($s.schema -isnot [int] -or $s.schema -ne 1 -or $s.owner -cne 'HermesSubscriberSetup' -or $s.sid -cne $Sid -or $s.home -cne $HomeDir -or $s.repo -cne $Repo -or $s.revision -cne $Pin -or $Pin -notmatch '^[0-9a-f]{40}$') { throw 'identity' }
        if ($s.phase -notin @('installing','awaiting_api','configuring','completed')) { throw 'phase' }
        if ($s.phase -eq 'installing') {
            $f=@($s.fingerprints)
            if ($f.Count -ne 0 -and ($f.Count -ne 4 -or $f[0] -cnotmatch '^[0-9A-F]{64}$' -or $f[1] -cnotmatch '^(starting|uv|git|node|system-packages|repository|python|venv|dependencies|node-deps|desktop|path|config-templates|platform-sdks|bootstrap-marker|configure|gateway)$' -or $f[2] -cnotmatch '^[0-9]+$' -or $f[3] -cnotmatch '^[0-9]+$')) { throw 'snapshot' }
        }
        else {
            if ($s.fingerprints -isnot [array] -or $s.fingerprints.Count -ne 9) { throw 'snapshot' }
            foreach ($v in $s.fingerprints) { if ($v -isnot [string] -or $v -cnotmatch '^(absent|[0-9A-F]{64})$') { throw 'hash' } }
        }
        return $s
    } catch { Fail 'CONFIG' 'Checkpoint отсутствует, повреждён или принадлежит другой установке. Данные не изменены.' }
}
function Assert-JournalSnapshot($State) {
    $current=Journal-Fingerprints $State.home $State.repo
    if (($current -join ',') -cne ($State.fingerprints -join ',')) { Fail 'CONFIG' 'Файлы установки или настройки изменены извне. Автоматическая перезапись запрещена.' }
}
# Hash all paths/content, not just the nine API-retry gate files. Never follow links.
function Install-TreeHash($HomeDir) {
    $null=Assert-SafePath $HomeDir
    $rows=New-Object 'Collections.Generic.List[string]'
    if (Test-Path -LiteralPath $HomeDir) {
        if (-not (Test-Path -LiteralPath $HomeDir -PathType Container)) { Fail 'CONFIG' 'Папка незавершённой установки повреждена. Файлы не тронуты; обратитесь в поддержку.' }
        $pending=New-Object 'Collections.Generic.Stack[string]'; $pending.Push($HomeDir)
        $rows.Add('root')
        while ($pending.Count) {
            foreach ($item in (Get-ChildItem -LiteralPath $pending.Pop() -Force -ErrorAction Stop)) {
                $null=Assert-SafePath $item.FullName
                $relative=$item.FullName.Substring($HomeDir.Length)
                if ($item.PSIsContainer) { $rows.Add('d:'+ $relative); $pending.Push($item.FullName) }
                else { $rows.Add('f:'+ $relative + ':' + (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256 -ErrorAction Stop).Hash) }
            }
        }
    } else { $rows.Add('absent') }
    $rows.Sort([StringComparer]::Ordinal)
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($rows|ConvertTo-Json -Compress))))).Replace('-','') } finally { $sha.Dispose() }
}
# Stage snapshots hash the whole install tree (node_modules/venv/.git: tens of
# thousands of files). Running that synchronously inside the child pipe reader
# stalls stdout, suppresses the elapsed heartbeat and can make the install
# watchdog misfire as NETWORK while everything is healthy. The stage name is
# published immediately (cheap); the expensive tree hash runs in a background
# job. Only a finished job is adopted, and the previous digest stays
# authoritative until then -- the fail-closed behavior resume already requires.
$script:StageJob = $null
$script:StageJobStage = ''
$script:StageKnownHash = $null
function Save-InstallStage([string]$Stage) {
    if (-not $script:InstallState) { return }
    if ($script:StageKnownHash) {
        # Cheap publish of the stage name carrying the last completed tree hash.
        try {
            $script:InstallState.fingerprints=@($script:StageKnownHash,$Stage,[string]$script:InstallPid,[string]$script:InstallStarted)
            Write-Journal $script:InstallState
        } catch { }
    }
    if ($script:StageJob) {
        $st = $script:StageJob.State
        if ($st -eq 'Running' -or $st -eq 'NotStarted') { return }
        Adopt-StageJob
    }
    $script:StageJobStage = $Stage
    try {
        $checkpoint = Join-Path $script:CheckpointDir 'checkpoint.ps1'
        $script:StageJob = Start-Job -ScriptBlock {
            param($HomeDir, $Checkpoint)
            . $Checkpoint
            Install-TreeHash $HomeDir
        } -ArgumentList $script:InstallState.home, $checkpoint
    } catch { $script:StageJob = $null; return }
    if (-not $script:StageKnownHash) {
        # First stage of the run: the tree is still small and there is no earlier
        # digest to carry, so the first journal entry must be a real snapshot.
        try { Wait-Job -Job $script:StageJob -Timeout 180 | Out-Null } catch { }
        Adopt-StageJob
    }
}
function Adopt-StageJob {
    if (-not $script:StageJob) { return }
    $st = $script:StageJob.State
    # Never kill an in-flight hash: only a finished/failed job is handled here.
    if ($st -eq 'Running' -or $st -eq 'NotStarted') { return }
    $job = $script:StageJob; $script:StageJob = $null
    if ($st -ne 'Completed') { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue; return }
    try {
        $hash = [string](Receive-Job $job)
        $hash = $hash.Trim()
        if ($hash -cmatch '^[0-9A-F]{64}$' -and $script:InstallState) {
            $script:StageKnownHash = $hash
            $script:InstallState.fingerprints=@($hash,[string]$script:StageJobStage,[string]$script:InstallPid,[string]$script:InstallStarted)
            Write-Journal $script:InstallState
        }
    } catch { } finally { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue }
}
function Pump-StageJob { Adopt-StageJob }
function Wait-StageJob([int]$Seconds = 180) {
    if (-not $script:StageJob) { return }
    try { Wait-Job -Job $script:StageJob -Timeout $Seconds | Out-Null } catch { }
    Adopt-StageJob
    if ($script:StageJob) { Remove-Job -Job $script:StageJob -Force -ErrorAction SilentlyContinue; $script:StageJob = $null }
}
function Preserve-IncompleteInstall($State) {
    # An interrupted install (Cancel, closed window, network drop, crash) is parked
    # aside untouched and a clean copy of the same pinned version is installed.
    # Nothing from the parked tree is ever executed or reused, so its content does
    # not need to match the last checkpoint: that mismatch is the NORMAL state after
    # a cancel (the tree hash is taken in the background and lags), and failing on it
    # turned «Повторить» into a permanent «обратитесь в поддержку» (v0.1.2 review).
    # Kept guards: never move a tree another installer run is still writing, and
    # never move a profile that carries settings, keys or a completed install.
    $f=@($State.fingerprints)
    if ($f.Count -eq 4) {
        $child=Get-Process -Id ([int]$f[2]) -ErrorAction SilentlyContinue
        if ($child -and [string]$child.StartTime.ToUniversalTime().Ticks -ceq $f[3]) { Fail 'CONFIG' 'Предыдущая установка ещё выполняется. Дождитесь её окончания и повторите.' }
    }
    # During phase 'installing' the vendor only copies templates (install.ps1
    # Stage-ConfigTemplates: .env <- .env.example, config.yaml <- cli-config.yaml.example);
    # our key is written later by configure.py. So a template copy (or an empty .env)
    # holds no secret and may be parked; anything else might be the person's own
    # settings or keys and stops the automatic recovery. The bootstrap marker only
    # means the vendor finished before the interruption: still no key, safe to park.
    foreach ($pair in @(@('config.yaml','hermes-agent\cli-config.yaml.example'),@('.env','hermes-agent\.env.example'))) {
        $file=Join-Path $State.home $pair[0]
        if (-not (Test-Path -LiteralPath $file)) { continue }
        $bytes=[IO.File]::ReadAllBytes($file)
        $template=Join-Path $State.home $pair[1]
        $isTemplate=($bytes.Length -eq 0) -or ((Test-Path -LiteralPath $template -PathType Leaf) -and ([Convert]::ToBase64String($bytes) -ceq [Convert]::ToBase64String([IO.File]::ReadAllBytes($template))))
        if (-not $isTemplate) { Fail 'CONFIG' 'Обнаружены файлы настроек или завершения установки. Автоматическое восстановление отменено; файлы сохранены.' }
    }
    if (Test-Path -LiteralPath $State.home) {
        $park=$State.home+'.subscriber-preserved-'+[Guid]::NewGuid().ToString('N')
        $null=Assert-SafePath $park
        # Same-volume no-overwrite rename. Do not rerun vendor over any existing tree.
        [IO.Directory]::Move($State.home,$park)
        Send-Event @{type='progress';message=('Прошлая установка была прервана. Её файлы отложены в ' + $park + ' (их можно удалить). Ставлю заново.')}
    }
    $State.fingerprints=@(); Write-Journal $State
}
# Standalone configure authorization; does not accept UI freshness assertions.
if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference='Stop'
    function Fail($Code,$Message) { throw $Message }
    try {
        . (Join-Path $PSScriptRoot 'checkpoint.ps1')
        $h=Assert-SafePath $args[0]; $r=Assert-SafePath $args[1]
        $pin=(Get-Content (Join-Path $PSScriptRoot 'upstream\commit.txt') -Raw).Trim()
        $s=Read-Journal $h $r $pin ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
        if ($s.phase -cne 'configuring') { throw 'phase' }
        Assert-JournalSnapshot $s
        [Console]::WriteLine('authorized')
        exit 0
    } catch { exit 1 }
}
