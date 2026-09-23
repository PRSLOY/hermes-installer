param([string]$Backend)
$ErrorActionPreference='Stop'
. (Join-Path $Backend 'worker.ps1')
$passed = 0
function Expect-Failure($Block, $Code) {
    $caught = $false
    try { & $Block } catch {
        if ($_.Exception.Data['code'] -ne $Code) { throw }
        $caught = $true
    }
    if (-not $caught) { throw 'Expected failure did not occur' }
    $script:passed++
}
Expect-Failure { Check-InstallResult @{Code=1;Text='secret-test-key'} } 'INSTALL'
Expect-Failure { Check-InstallResult @{Code=0;Text='not-json'} } 'INSTALL'
$events = @('node','desktop','dependencies','repository','venv') | ForEach-Object { @{stage=$_;ok=$true;skipped=$false}|ConvertTo-Json -Compress }
$events += '{"protocol_version":1,"ok":true}'
Check-InstallResult @{Code=0;Text=($events -join "`n")}; $passed++
$events[1] = '{"stage":"desktop","ok":true,"skipped":true}'
Expect-Failure { Check-InstallResult @{Code=0;Text=($events -join "`n")} } 'INSTALL'
Expect-Failure { Check-Desktop (Join-Path $env:TEMP ([guid]::NewGuid().ToString())) } 'VERIFY'
Write-Output "offline_helpers_passed=$passed"
