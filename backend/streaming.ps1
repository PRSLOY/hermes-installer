# Bounded, concurrent pipe reads: no PowerShell event callbacks on worker threads.
function Publish-UpstreamStage([string]$Line) {
    try { $v = $Line | ConvertFrom-Json } catch { return }
    $names = @('uv','git','node','system-packages','repository','python','venv','dependencies','node-deps','desktop','path','config-templates','platform-sdks','bootstrap-marker','configure','gateway')
    $titles = @('Менеджер пакетов uv','Git','Node.js','Системные утилиты','Репозиторий Hermes','Python','Окружение Python','Зависимости Python','Зависимости Node.js','Сборка Desktop','Пути запуска','Шаблоны настроек','SDK платформ','Маркер установки','Мастер upstream (неинтерактивный режим)','Gateway upstream (неинтерактивный режим)')
    $index = [Array]::IndexOf($names, [string]$v.stage)
    if ($index -lt 0 -or $v.ok -isnot [bool] -or $v.skipped -isnot [bool]) { return }
    if (Get-Command Save-InstallStage -ErrorAction SilentlyContinue) { Save-InstallStage $names[$index] }
    $state = if (-not $v.ok) { 'ошибка' } elseif ($v.skipped) { 'пропущен upstream' } else { 'завершён upstream' }
    # Never echo raw reason, native output or any provider-controlled content.
    Send-Event @{type='progress';stage=$names[$index];index=($index+1);count=$names.Count;message=($titles[$index] + ': ' + $state + '. Ожидаем дальнейшие события установщика.')}
}
function Read-ChildPipes($Process, [string]$InputText, [int]$Seconds, [bool]$StreamStages) {
    $outBuffer = New-Object char[] 4096
    $errBuffer = New-Object char[] 4096
    $outTask = $Process.StandardOutput.ReadAsync($outBuffer,0,$outBuffer.Length)
    $errTask = $Process.StandardError.ReadAsync($errBuffer,0,$errBuffer.Length)
    $inputBytes = $utf8.GetBytes($InputText)
    $writeTask = $Process.StandardInput.BaseStream.WriteAsync($inputBytes,0,$inputBytes.Length)
    $inputClosed = $false
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $nextBeat = 5
    $text = New-Object Text.StringBuilder
    $line = New-Object Text.StringBuilder
    $overflow = $false
    try {
        while ($null -ne $outTask -or $null -ne $errTask -or -not $Process.HasExited) {
            if ($clock.Elapsed.TotalSeconds -ge $Seconds) { return @{TimedOut=$true} }
            if (-not $inputClosed -and $writeTask.IsCompleted) {
                $writeTask.GetAwaiter().GetResult(); $Process.StandardInput.Close(); $inputClosed=$true
                [Array]::Clear($inputBytes,0,$inputBytes.Length)
            }
            if ($null -ne $outTask -and $outTask.IsCompleted) {
                $n = $outTask.GetAwaiter().GetResult()
                if ($n -eq 0) { $outTask=$null } else {
                    $chunk = New-Object string($outBuffer,0,$n)
                    if (-not $StreamStages) {
                        if ($text.Length + $n -gt 2097152) { Fail 'VERIFY' 'Ответ дочернего процесса слишком велик. Проверка остановлена.' }
                        [void]$text.Append($chunk)
                    } else {
                        foreach ($c in $chunk.ToCharArray()) {
                            if ($c -eq "`n") {
                                if (-not $overflow) {
                                    $s = $line.ToString().TrimEnd("`r")
                                    Publish-UpstreamStage $s
                                    # Preserve only bounded JSON frames for final upstream verification.
                                    if ($s.StartsWith('{') -and $text.Length -lt 1048576) { [void]$text.AppendLine($s) }
                                }
                                [void]$line.Clear(); $overflow=$false
                            } elseif ($line.Length -lt 32768) { [void]$line.Append($c) } else { $overflow=$true }
                        }
                    }
                    $outTask=$Process.StandardOutput.ReadAsync($outBuffer,0,$outBuffer.Length)
                }
            }
            if ($null -ne $errTask -and $errTask.IsCompleted) {
                $n = $errTask.GetAwaiter().GetResult()
                if ($n -eq 0) { $errTask=$null } else { $errTask=$process.StandardError.ReadAsync($errBuffer,0,$errBuffer.Length) }
            }
            # Adopt completed stage snapshots without blocking the child's pipes.
            if (Get-Command Pump-StageJob -ErrorAction SilentlyContinue) { Pump-StageJob }
            if ($clock.Elapsed.TotalSeconds -ge $nextBeat) {
                $label = if ($StreamStages) { 'Официальный установщик' } elseif ($script:ChildLabel) { $script:ChildLabel } else { 'Проверка API и Hermes' }
                Send-Event @{type='progress';message=($label + ': процесс выполняется. Прошло ' + [int]$clock.Elapsed.TotalSeconds + ' с. Объём загрузки неизвестен; ожидаем результат.')}
                $nextBeat = $clock.Elapsed.TotalSeconds + 5
            }
            Start-Sleep -Milliseconds 20
        }
        $Process.WaitForExit()
        return @{Code=$Process.ExitCode;Text=$text.ToString();TimedOut=$false}
    } finally { [Array]::Clear($inputBytes,0,$inputBytes.Length) }
}
