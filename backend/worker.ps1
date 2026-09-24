#requires -version 5.1
# Protocol v1; no secrets in arguments, files, child installer environment or logs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Send-Event($Event) { [Console]::WriteLine(($Event | ConvertTo-Json -Compress -Depth 8)) }
function Fail([string]$Code, [string]$Message) {
    $e = New-Object System.Exception($Message)
    $e.Data['code'] = $Code
    throw $e
}
function Quote-Arg([string]$Value) {
    # Windows CommandLineToArgvW escaping, not shell quoting.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
. (Join-Path $PSScriptRoot 'streaming.ps1')
. (Join-Path $PSScriptRoot 'checkpoint.ps1')
function Run-Child([string]$Exe, [string[]]$Arguments, [string]$InputText = '', [int]$Seconds = 1200, [switch]$StreamStages) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = (($Arguments | ForEach-Object { Quote-Arg $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8
    # Do not let inherited profile routing select another profile.
    $psi.EnvironmentVariables.Remove('HERMES_PROFILE')
    $psi.EnvironmentVariables.Remove('HERMES_INFERENCE_MODEL')
    $psi.EnvironmentVariables.Remove('HERMES_INFERENCE_PROVIDER')
    $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    try {
        [void]$p.Start()
        if ($StreamStages -and $script:InstallState) {
            $script:InstallPid=$p.Id; $script:InstallStarted=$p.StartTime.ToUniversalTime().Ticks
            # Record child identity immediately; stage frames replace the initial snapshot.
            $script:InstallState.fingerprints=@($script:InstallInitialHash,'starting',[string]$script:InstallPid,[string]$script:InstallStarted)
            Write-Journal $script:InstallState
        }
        $result = Read-ChildPipes $p $InputText $Seconds $StreamStages.IsPresent
        if ($result.TimedOut) {
            # Kill the entire owned child tree; never orphan npm/git/runtime children.
            $kill = New-Object System.Diagnostics.ProcessStartInfo
            $kill.FileName = "$env:SystemRoot\System32\taskkill.exe"
            $kill.Arguments = "/PID $($p.Id) /T /F"
            $kill.UseShellExecute = $false; $kill.CreateNoWindow = $true
            $kill.RedirectStandardOutput = $true; $kill.RedirectStandardError = $true
            $kp = [Diagnostics.Process]::Start($kill)
            $ko = $kp.StandardOutput.ReadToEndAsync(); $ke = $kp.StandardError.ReadToEndAsync()
            $kp.WaitForExit(); $kp.Dispose()
            Fail 'NETWORK' 'Операция превысила время ожидания и остановлена. Проверьте сеть и повторите установку.'
        }
        return $result
    } finally { $p.Dispose() }
}
function Check-InstallResult($Result) {
    $events = @()
    foreach ($line in ($Result.Text -split "`n")) {
        try { $v = $line | ConvertFrom-Json; if ($null -ne $v.ok) { $events += $v } } catch { }
    }
    if ($Result.Code -ne 0) {
        # Only allowlisted stage IDs and numeric exit status may leave the child boundary.
        # Raw reason/stderr may contain credentials or private paths: never echo them.
        $known = @('uv','git','node','system-packages','repository','python','venv','dependencies','node-deps','desktop','path','config-templates','platform-sdks','bootstrap-marker','configure','gateway')
        $failed = @($events | Where-Object { $_.ok -is [bool] -and -not $_.ok -and $_.stage -cin $known })
        $stageName = if ($failed.Count) { [string]$failed[-1].stage } else { 'unknown' }
        $exitCode = [int]$Result.Code
        $reason = if ($failed.Count) { [string]$failed[-1].reason } else { '' }
        # Classify the upstream reason against a FIXED allowlist and add one of our own
        # sentences. The reason text itself is never echoed; the user gets an actionable
        # cause instead of "check free disk space" for what is usually a network failure.
        $advice = ''
        $r = $reason.ToLowerInvariant()
        # Stage-specific cause first: a concrete sentence per known stage beats guessing
        # from a generic reason, so a novice never reads "check free disk space".
        $stageAdvice = @{
            'uv'               = 'Не удалось установить менеджер пакетов uv (скачивание). Проверьте интернет/VPN и повторите.'
            'git'              = 'Не удалось установить Git. Проверьте интернет и повторите.'
            'node'             = 'Не удалось получить Node.js. Проверьте интернет и повторите.'
            'system-packages'  = 'Не удалось установить системные утилиты. Проверьте интернет и повторите.'
            'repository'       = 'Скачивание кода Hermes не удалось. Проверьте интернет или VPN и повторите.'
            'python'           = 'Не удалось подготовить Python. Повторите установку.'
            'venv'             = 'Не удалось создать окружение Python. Повторите установку.'
            'dependencies'     = 'Не удалось установить Python-зависимости. Проверьте интернет и повторите.'
            'node-deps'        = 'Не удалось установить Node.js-зависимости. Проверьте интернет и повторите.'
            'desktop'          = 'Не удалось собрать Desktop (загрузка компонентов). Проверьте интернет и повторите.'
        }
        if ($r -match 'failed to download repository' -or $r -match 'rpc failed' -or $r -match 'early eof' -or $r -match 'etimedout' -or $r -match 'econnreset') {
            $advice = ' Скачивание не удалось из-за сети. Проверьте интернет или VPN и повторите.'
        } elseif ($r -match 'git fetch') {
            $advice = ' Не удалось получить код Hermes из сети. Проверьте интернет/VPN и повторите.'
        } elseif ($r -match 'git checkout') {
            $advice = ' Не удалось переключить код Hermes на нужную версию. Повторите; если ошибка повторяется, обратитесь в поддержку.'
        } elseif ($stageAdvice.ContainsKey($stageName)) {
            $advice = ' ' + $stageAdvice[$stageName]
        } elseif ($r -match 'electron') {
            $advice = ' Не удалось загрузить компоненты Desktop из-за сети. Проверьте интернет и повторите.'
        } elseif ($r -match 'npm') {
            $advice = ' Не удалось установить Node.js-зависимости. Проверьте интернет и повторите.'
        }
        Fail 'INSTALL' ("Официальная установка остановлена: этап $stageName, код $exitCode.$advice Проверка API ещё не запускалась. Сохраните этот код для диагностики; не удаляйте папку установки.")
    }
    if (-not ($events | Where-Object { $_.protocol_version -eq 1 -and $_.ok -eq $true })) {
        Fail 'INSTALL' 'Официальный установщик не подтвердил завершение. Установка не считается успешной.'
    }
    foreach ($stage in @('node','desktop','dependencies','repository','venv')) {
        $entry = @($events | Where-Object { $_.stage -eq $stage })
        if ($entry.Count -ne 1 -or $entry[0].ok -ne $true -or $entry[0].skipped -eq $true) {
            Fail 'INSTALL' 'Обязательный этап официальной установки пропущен или завершился ошибкой. Проверьте сеть и повторите.'
        }
    }
}
function Check-Desktop([string]$Repo) {
    $release = Join-Path $Repo 'apps\desktop\release'
    $name = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'win-arm64-unpacked' } else { 'win-unpacked' }
    $dir = Join-Path $release $name
    foreach ($relative in @('Hermes.exe','resources\app.asar','icudtl.dat')) {
        $file = Join-Path $dir $relative
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -eq 0) {
            Fail 'VERIFY' 'Файлы Hermes Desktop не готовы. Установите официальный Hermes Desktop и повторите подключение.'
        }
    }
    $stream = [IO.File]::OpenRead((Join-Path $dir 'Hermes.exe'))
    try { if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90) { Fail 'VERIFY' 'Исполняемый файл Desktop повреждён. Повторите официальную установку.' } } finally { $stream.Dispose() }
    # Return the PACKED launcher, not `hermes desktop`: the CLI rebuilds the desktop
    # (npm install + electron-builder) on every launch, which costs minutes. The
    # official installer points its shortcuts at this exe for the same reason.
    return (Join-Path $dir 'Hermes.exe')
}
# --- Voice: Microsoft Visual C++ x64 runtime --------------------------------
# faster-whisper's ctranslate2.dll needs vcruntime140/vcruntime140_1/msvcp140; a clean
# Windows lacks them (sandbox 2026-09-23). Official redistributable, Authenticode-checked,
# one UAC prompt via -Verb RunAs (never bypassed). Never terminal.
$script:VcRuntimeDlls = @('vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll')
$script:VcRedistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
function Test-VcRuntime([string]$SystemDir = (Join-Path $env:WINDIR 'System32')) {
    foreach ($dll in $script:VcRuntimeDlls) { if (-not (Test-Path -LiteralPath (Join-Path $SystemDir $dll) -PathType Leaf)) { return $false } }
    return $true
}
function Test-MicrosoftSignature($Signature) {
    if ($null -eq $Signature -or [string]$Signature.Status -cne 'Valid' -or $null -eq $Signature.SignerCertificate) { return $false }
    # Exact organisation RDN, not a substring: "O=Microsoft Corporation Evil" must fail.
    return ([string]$Signature.SignerCertificate.Subject -cmatch '(^|,\s*)O=Microsoft Corporation(\s*,|$)')
}
function Get-VcRedistResult([int]$ExitCode) {
    # 3010 = installed, reboot pending; 1638 = same/newer version already installed.
    if ($ExitCode -in @(0, 3010, 1638)) { return 'installed' }
    return 'failed'
}
function Install-VcRuntime([string]$SystemDir = (Join-Path $env:WINDIR 'System32'), [int]$InstallSeconds = 600) {
    if (Test-VcRuntime $SystemDir) { return 'present' }
    $dir = Join-Path ([IO.Path]::GetTempPath()) ('hermes-vcredist-' + [Guid]::NewGuid().ToString('N'))
    try {
        [void][IO.Directory]::CreateDirectory($dir)
        $file = Join-Path $dir 'vc_redist.x64.exe'
        # Retry: the redist is ~25 MB and one dropped connection would otherwise fail the
        # optional voice component. The file is Authenticode-checked below before it runs.
        $downloaded = $false
        for ($attempt = 1; $attempt -le 3 -and -not $downloaded; $attempt++) {
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri $script:VcRedistUrl -OutFile $file -TimeoutSec 180
                $downloaded = $true
            } catch {
                if ($attempt -lt 3) { Start-Sleep -Seconds (5 * $attempt) }
            }
        }
        if (-not $downloaded) { return 'network' }
        $size = (Get-Item -LiteralPath $file).Length
        if ($size -lt 5MB -or $size -gt 100MB) { return 'signature' }
        if (-not (Test-MicrosoftSignature (Get-AuthenticodeSignature -LiteralPath $file))) { return 'signature' }
        try {
            $p = Start-Process -FilePath $file -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -PassThru -WindowStyle Hidden
        } catch { return 'declined' }   # UAC "No" -> Win32 1223 "canceled by the user"
        if (-not $p.WaitForExit($InstallSeconds * 1000)) { return 'failed' }
        if ((Get-VcRedistResult $p.ExitCode) -ne 'installed') { return 'failed' }
        if (-not (Test-VcRuntime $SystemDir)) { return 'failed' }
        return 'installed'
    } catch { return 'failed' }
    finally { try { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction Stop } catch { } }
}
$script:VcRuntimeText = @{
    'start'     = 'Включаю распознавание голоса: Windows попросит разрешение на установку компонента Microsoft Visual C++.'
    'installed' = 'Распознавание голосовых сообщений включено.'
    'declined'  = 'Распознавание голоса не включено: установка компонента Microsoft отменена; Hermes работает.'
    'network'   = 'Распознавание голоса не включено: не удалось скачать компонент Microsoft; Hermes работает.'
    'signature' = 'Распознавание голоса не включено: файл компонента не прошёл проверку подписи Microsoft; Hermes работает.'
    'failed'    = 'Распознавание голоса не включено: установщик компонента Microsoft завершился с ошибкой; Hermes работает.'
}
function Invoke-TelegramAction([string]$Action, $InputData, [string]$HomeDir, [string]$Repo, [string]$Python, [string]$Pin, [string]$Sid, [string]$Journal) {
    # Only a home this installer completed: never touch a foreign or half-done Hermes.
    if (-not (Test-Path -LiteralPath $Journal) -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { Fail 'CONFIG' 'Hermes не установлен этим установщиком. Телеграм здесь не настраивается.' }
    $state = Read-Journal $HomeDir $Repo $Pin $Sid
    if ($state.phase -ne 'completed') { Fail 'CONFIG' 'Установка Hermes не завершена. Сначала завершите установку.' }
    $mode = if ($Action -ceq 'telegram_approve') { 'approve' } else { 'pending' }
    $payload = if ($mode -eq 'approve') { @{request_id=[string]$InputData.request_id; user_id=[string]$InputData.user_id} | ConvertTo-Json -Compress } else { '{}' }
    $status = 'failed'; $requests = @()
    try {
        # approve = pairing approve + home channel + gateway restart + connected wait (<= ~4 min).
        $child = Run-Child $Python @((Join-Path $PSScriptRoot 'telegram.py'),$HomeDir,$Repo,$mode) $payload 300
        $last = @($child.Text -split "`n" | Where-Object { $_.Trim() })[-1]
        $reply = $last | ConvertFrom-Json
        $allowed = if ($mode -eq 'approve') { @('approved','approved_partial','expired','off','failed') } else { @('pending','none','done','off','failed') }
        if ([string]$reply.status -cin $allowed) { $status = [string]$reply.status }
        if ($mode -eq 'pending' -and $status -eq 'pending') {
            foreach ($r in @($reply.requests)) {
                if ($requests.Count -ge 10) { break }
                if ($r.id -isnot [string] -or $r.id -cnotmatch '^[0-9a-f]{16}$' -or $r.user_id -isnot [string] -or $r.user_id -cnotmatch '^[0-9]{1,20}$') { continue }
                $name = if ($r.name -is [string]) { ($r.name -replace '[\x00-\x1F\x7F]', '') } else { '' }
                if ($name.Length -gt 64) { $name = $name.Substring(0, 64) }
                $user = if ($r.username -is [string] -and $r.username -cmatch '^[A-Za-z0-9_]{5,64}$') { $r.username } else { '' }
                $requests += @{id=$r.id; user_id=$r.user_id; name=$name; username=$user}
            }
            if ($requests.Count -eq 0) { $status = 'failed' }
        }
    } catch { $status = 'failed' }
    $text = @{
        'pending'          = 'Боту написали. Выберите себя, чтобы бот отвечал только вам.'
        'none'             = 'Пока сообщений нет — напишите боту и нажмите «Проверить» ещё раз.'
        'done'             = 'Телеграм уже подключён к вам. Пишите боту — он ответит.'
        'approved'         = 'Готово! Бот прислал вам приветствие в Телеграм — пишите ему, что нужно сделать. Если в Hermes в «Сообщениях» на пару секунд видно «disconnected» — это бот перезапускается, подождите.'
        'approved_partial' = 'Доступ выдан — пишите боту, он ответит. Перезапуск бота не завершился; если бот попросит «/sethome», перезагрузите компьютер.'
        'expired'          = 'Этот запрос устарел. Напишите боту ещё раз и нажмите «Проверить».'
        'off'              = 'Телеграм-бот на этом компьютере не настроен.'
        'failed'           = 'Не удалось проверить Телеграм. Hermes работает; повторите через минуту.'
    }
    $message = $text[$status]
    if ($status -eq 'approved' -and $reply.welcomed -ne $true) { $message = 'Готово! Напишите боту в Телеграм — теперь он ответит. Если в Hermes в «Сообщениях» на пару секунд видно «disconnected» — это бот перезапускается, подождите.' }
    Send-Event @{type='telegram'; status=$status; message=$message; requests=[object[]]$requests}
    return 0
}
# --- Optional backup providers (issue #10) -----------------------------------
# 0-2 entries {provider_id, endpoint, model, api_key}, validated like the primary.
# Returned as hashtables for fallbacks.py only; configure.py never sees them.
# Throws on any malformed entry: the caller maps that to the INPUT error.
function Get-FallbackEntries($InputData) {
    $value = $InputData.fallbacks
    if ($null -eq $value) { return }
    if ($value -isnot [array] -or $value.Count -gt 2) { throw 'fallbacks' }
    $seenIds = @()
    $seenEndpoints = @(([string]$InputData.endpoint).TrimEnd('/').ToLowerInvariant())
    foreach ($fb in $value) {
        if ($fb -isnot [System.Management.Automation.PSCustomObject]) { throw 'fallbacks' }
        foreach ($name in @($fb.PSObject.Properties.Name)) { if ($name -cnotin @('provider_id','endpoint','model','api_key')) { throw 'fallbacks' } }
        if ($fb.provider_id -isnot [string] -or $fb.provider_id -cnotmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'fallbacks' }
        if ($fb.endpoint -isnot [string] -or $fb.api_key -isnot [string]) { throw 'fallbacks' }
        $uri = [Uri]$fb.endpoint
        if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne 'https' -or -not $uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'fallbacks' }
        if ($fb.api_key -cnotmatch '^[\x21-\x7E]{8,8192}$') { throw 'fallbacks' }
        $model = $fb.model
        if ($null -ne $model -and ($model -isnot [string] -or $model.Length -gt 256 -or $model -match '[\x00-\x1F]')) { throw 'fallbacks' }
        # Never the primary again, never the same provider twice.
        $endpointKey = $fb.endpoint.TrimEnd('/').ToLowerInvariant()
        if ($endpointKey -cin $seenEndpoints -or $fb.provider_id -cin $seenIds) { throw 'fallbacks' }
        $seenEndpoints += $endpointKey; $seenIds += $fb.provider_id
        @{provider_id=$fb.provider_id; endpoint=$fb.endpoint; model=[string]$model; api_key=$fb.api_key}
    }
}
$script:FallbackText = @{
    'auth'    = 'ключ отклонён'
    'quota'   = 'на ключе нет средств или исчерпан лимит'
    'network' = 'API провайдера не отвечает'
    'verify'  = 'API провайдера ответил некорректно'
    'failed'  = 'не удалось сохранить настройку'
}
function Main {
    $mutex = $null; $locked = $false; $fallbackEntries = @()
    try {
        try {
            # .NET Framework may prepend a UTF-8 BOM to redirected stdin when the
            # parent's console/ANSI code page is UTF-8 (reproduced in a clean
            # Windows Sandbox: first char U+FEFF). ConvertFrom-Json rejects it.
            $raw = [Console]::In.ReadToEnd().TrimStart([char]0xFEFF)
            if ($raw.Length -gt 32768) { throw 'size' }
            $inputData = $raw | ConvertFrom-Json
            $telegramAction = $null
            if ($inputData.protocol -eq 1 -and $inputData.action -cin @('telegram_pending','telegram_approve')) {
                # Short post-install actions (Done screen). No key, endpoint or token travels here.
                $telegramAction = [string]$inputData.action
                $allowedFields = if ($telegramAction -ceq 'telegram_approve') { @('protocol','action','request_id','user_id') } else { @('protocol','action') }
                foreach ($name in @($inputData.PSObject.Properties.Name)) { if ($name -cnotin $allowedFields) { throw 'field' } }
                if ($telegramAction -ceq 'telegram_approve' -and ($inputData.request_id -isnot [string] -or $inputData.request_id -cnotmatch '^[0-9a-f]{16}$' -or $inputData.user_id -isnot [string] -or $inputData.user_id -cnotmatch '^[0-9]{1,20}$')) { throw 'request' }
            }
        } catch { Fail 'INPUT' 'Проверьте данные: точный HTTPS-адрес API, API-ключ, необязательное имя модели, токен Телеграм-бота и запасные ключи.' }
        try {
          if (-not $telegramAction) {
            if ($inputData.protocol -ne 1 -or $inputData.action -cne 'install') { throw 'protocol' }
            if ($inputData.endpoint -isnot [string] -or $inputData.api_key -isnot [string]) { throw 'types' }
            $uri = [Uri]$inputData.endpoint
            if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne 'https' -or -not $uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'url' }
            if ($inputData.api_key -notmatch '^[\x21-\x7E]{8,8192}$') { throw 'key' }
            foreach ($field in @('model','provider_name')) {
                $value = $inputData.$field
                if ($null -ne $value -and ($value -isnot [string] -or $value.Length -gt 256 -or $value -match '[\x00-\x1F]')) { throw 'field' }
            }
            # Optional Telegram bot token: absent/empty = skipped; otherwise digits:secret.
            $tgValue = $inputData.telegram_bot_token
            if ($null -ne $tgValue -and ($tgValue -isnot [string] -or ($tgValue.Length -gt 0 -and $tgValue -cnotmatch '^[0-9]{1,20}:[A-Za-z0-9_-]{30,64}$'))) { throw 'telegram' }
            $fallbackEntries = @(Get-FallbackEntries $inputData)
          }
        } catch { Fail 'INPUT' 'Проверьте данные: точный HTTPS-адрес API, API-ключ, необязательное имя модели, токен Телеграм-бота и запасные ключи.' }
        # The bot token goes only to telegram.py; configure.py never sees it.
        $telegramToken = [string]$inputData.telegram_bot_token
        $tgValue = $null
        $inputData.PSObject.Properties.Remove('telegram_bot_token')
        # Backup keys go only to fallbacks.py (after the primary is verified); never to configure.py.
        $inputData.PSObject.Properties.Remove('fallbacks')
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $mutex = New-Object Threading.Mutex($false, "Local\HermesSubscriberSetup-$sid")
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { Fail 'BUSY' 'Другая установка уже выполняется. Дождитесь её завершения.' }
        if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { Fail 'UNSUPPORTED' 'Нужна 64-разрядная Windows 10/11.' }
        $homeDir = Join-Path $env:LOCALAPPDATA 'hermes'
        if ($env:HERMES_HOME -and [IO.Path]::GetFullPath($env:HERMES_HOME).TrimEnd('\') -ine [IO.Path]::GetFullPath($homeDir).TrimEnd('\')) {
            Fail 'CONFIG' 'Обнаружен нестандартный профиль Hermes. Чтобы его не повредить, автоматическая настройка остановлена.'
        }
        $repo = Join-Path $homeDir 'hermes-agent'
        $exe = Join-Path $homeDir 'bin\hermes.exe'
        $python = Join-Path $repo 'venv\Scripts\python.exe'
        $homeDir=Assert-SafePath $homeDir; $repo=Assert-SafePath $repo
        $pin = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'upstream\commit.txt') -Raw).Trim()
        $journal=Journal-Path $homeDir; $null=Assert-SafePath $journal
        if ($telegramAction) { return (Invoke-TelegramAction $telegramAction $inputData $homeDir $repo $python $pin $sid $journal) }
        $fresh = -not (Test-Path -LiteralPath $homeDir)
        $resume=$false
        if (Test-Path -LiteralPath $journal) {
            $state=Read-Journal $homeDir $repo $pin $sid
            if ($state.phase -eq 'installing') { Preserve-IncompleteInstall $state; $resume=$true }
        }
        if ($resume -or ($fresh -and -not (Test-Path -LiteralPath $journal))) {
            if ($pin -notmatch '^[0-9a-f]{40}$') { Fail 'INSTALL' 'Некорректная версия пакета.' }
            $state=[pscustomobject]@{schema=1;owner='HermesSubscriberSetup';sid=$sid;home=$homeDir;repo=$repo;revision=$pin;phase='installing';fingerprints=@()}
            if ($resume) { Write-Journal $state } else { Write-Journal $state -New }
            # An absent target is the only permitted entry to upstream installation.
            if (Test-Path -LiteralPath $homeDir) { Fail 'CONFIG' 'Папка появилась извне. Установка остановлена.' }
            Send-Event @{type='progress';message='Устанавливаю официальный Hermes и Desktop. Это может занять 10–20 минут; возможен системный запрос разрешения.'}
            $installer = Join-Path $PSScriptRoot 'upstream\install.ps1'
            $expected = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'upstream\install.sha256') -Raw).Trim()
            if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ine $expected) { Fail 'INSTALL' 'Контрольная сумма официального установщика не совпала. Скачайте пакет заново.' }
            $script:InstallState=$state; $script:InstallInitialHash=Install-TreeHash $homeDir
            $result = Run-Child "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$installer,'-NonInteractive','-SkipSetup','-IncludeDesktop','-Json','-Commit',$pin,'-HermesHome',$homeDir,'-InstallDir',$repo) '' 2400 -StreamStages
            Check-InstallResult $result
            # The last stage hashes in the background; adopt it before re-fingerprinting.
            Wait-StageJob
            $fingerprints=Journal-Fingerprints $homeDir $repo
            if (-not (Test-Path -LiteralPath $exe -PathType Leaf) -or -not (Test-Path -LiteralPath $python -PathType Leaf)) { Fail 'INSTALL' 'Не найден рабочий Hermes или Python.' }
            # Revision pin. A real git clone leaves HEAD detached at the pinned commit.
            # A github-blocked install reaches the same pinned archive through a proxy
            # and records the pin in a marker file instead (install.ps1 ZIP path).
            $headPin = ''
            try { $h = Join-Path $repo '.git\HEAD'; if (Test-Path -LiteralPath $h) { $headPin = (Get-Content -LiteralPath $h -Raw).Trim() } } catch { }
            $markerPin = ''
            try { $m = Join-Path $repo '.hermes-subscriber-pin'; if (Test-Path -LiteralPath $m) { $markerPin = (Get-Content -LiteralPath $m -Raw).Trim() } } catch { }
            if ($headPin -cne $pin -and $markerPin -cne $pin) { Fail 'INSTALL' 'Ревизия установки не совпадает с пакетом.' }
            $desktopExe = Check-Desktop $repo
            $state.fingerprints=$fingerprints; $state.phase='awaiting_api'; Write-Journal $state
        } else {
            $state=Read-Journal $homeDir $repo $pin $sid
            if ($state.phase -eq 'installing') { Fail 'CONFIG' 'Незавершённая установка сохранена. Безопасное автоматическое восстановление пока недоступно; обратитесь в поддержку. Файлы не удалены.' }
            if ($state.phase -ne 'awaiting_api') { Fail 'CONFIG' 'Настройка уже завершена или прервана во время записи. Автоматическая перезапись запрещена.' }
            Assert-JournalSnapshot $state
            $desktopExe = Check-Desktop $repo
        }
        $state.phase='configuring'; Write-Journal $state
        Send-Event @{type='progress';message='Проверяю API и реальный ответ Hermes. Проверка расходует небольшую квоту провайдера.'}
        $inputData.PSObject.Properties.Remove('fresh')
        try {
        # Bounded above the worst healthy probe chain: up to 3 transport attempts
        # of 45 s per request, /models plus up to 3 candidate probes, plus the
        # agent's own run budget. A merely slow provider must not surface as NETWORK.
        $helper = Run-Child $python @((Join-Path $PSScriptRoot 'configure.py'),$homeDir,$repo) ($inputData | ConvertTo-Json -Compress -Depth 8) 900
        } catch {
            if ($_.Exception.Data['code'] -eq 'NETWORK') {
                Assert-JournalSnapshot $state
                $state.phase='awaiting_api'; Write-Journal $state
            }
            throw
        }
        try { $reply = $helper.Text | ConvertFrom-Json } catch { Fail 'VERIFY' 'Проверка Hermes не вернула корректный результат. Настройка не подтверждена.' }
        if ($helper.Code -ne 0 -or $reply.ok -ne $true) {
            $allowed = @('AUTH','NETWORK','QUOTA','CONFIG','VERIFY','INPUT')
            $code = if ($reply.code -in $allowed) { $reply.code } else { 'VERIFY' }
            # Helper emits fixed messages only; never native exception/provider body.
            if ($code -in @('AUTH','QUOTA','NETWORK')) {
                Assert-JournalSnapshot $state
                $state.phase='awaiting_api'; Write-Journal $state
            }
            Fail $code ([string]$reply.message)
        }
        # Config is written and verified: record completion BEFORE the optional
        # set, so a kill during the up-to-180 s set step cannot leave the journal
        # in 'configuring' (review 2026-09-22). The success event still comes last.
        $state.phase='completed'; Write-Journal $state
        # Out-of-box set: SOUL, first-step skill, keyless MCP catalogs. A failure
        # here is never terminal -- one warning line, the verified success stands.
        # Child output is inspected, never relayed; the step owns its own try/catch.
        Send-Event @{type='progress';message='Настраиваю набор: личность помощника, первые шаги, калькулятор и справочники…'}
        try {
            $script:ChildLabel = 'Настройка набора'
            $extra = Run-Child $python @((Join-Path $PSScriptRoot 'extras.py'),$homeDir,$repo,(Join-Path $PSScriptRoot 'assets')) '' 180
            $extraOk = $false
            if ($extra.Code -eq 0) {
                try { $extraOk = (($extra.Text | ConvertFrom-Json).ok -eq $true) } catch { $extraOk = $false }
            }
            if (-not $extraOk) { Send-Event @{type='progress';message='Набор настроен не полностью; Hermes работает.'} }
        } catch {
            Send-Event @{type='progress';message='Набор настроен не полностью; Hermes работает.'}
        }
        # Marketplaces search MCP (ru-marketplace-mcp): its own step and budget, since
        # the pinned archive download plus uv sync (Python 3.12 + deps) can take
        # minutes on a slow line. Same contract as the set: never terminal, fixed lines.
        Send-Event @{type='progress';message='Подключаю поиск по маркетплейсам…'}
        $mpStatus = 'failed'
        try {
            $script:ChildLabel = 'Поиск по маркетплейсам'
            $mp = Run-Child $python @((Join-Path $PSScriptRoot 'extras.py'),$homeDir,$repo,(Join-Path $PSScriptRoot 'assets'),'--marketplaces') '' 900
            if ($mp.Code -eq 0) {
                try { $mpStatus = [string](($mp.Text | ConvertFrom-Json).marketplaces) } catch { $mpStatus = 'failed' }
            }
        } catch { $mpStatus = 'failed' }
        $mpText = @{
            'added'  = 'Поиск по маркетплейсам подключён: Ozon, Авито, Яндекс Маркет, Детский мир.'
            'exists' = 'Поиск по маркетплейсам уже был подключён.'
            'failed' = 'Поиск по маркетплейсам не подключён; Hermes работает.'
        }
        if (-not $mpText.ContainsKey($mpStatus)) { $mpStatus = 'failed' }
        Send-Event @{type='progress';message=$mpText[$mpStatus]}
        # Optional backup providers (issue #10): Hermes' own fallback_providers chain.
        # Only after the primary is verified and the set ran; never terminal. Keys
        # travel on stdin to fallbacks.py only and are dropped right after; its
        # statuses map to fixed Russian lines, no provider text is relayed.
        $fallbackNote = ''
        if ($fallbackEntries.Count -gt 0) {
            Send-Event @{type='progress';message='Подключаю запасных провайдеров…'}
            $fbTotal = $fallbackEntries.Count
            $fbStatuses = @('failed') * $fbTotal
            $fbInput = $null
            try {
                $script:ChildLabel = 'Запасные провайдеры'
                $fbInput = @{fallbacks=[object[]]$fallbackEntries} | ConvertTo-Json -Compress -Depth 6
                # Up to 2 live checks of 3 x 45 s transport attempts each, plus the writes.
                $fb = Run-Child $python @((Join-Path $PSScriptRoot 'fallbacks.py'),$homeDir) $fbInput 420
                $fbLast = @($fb.Text -split "`n" | Where-Object { $_.Trim() })[-1]
                $fbResults = @(($fbLast | ConvertFrom-Json).results)
                for ($i = 0; $i -lt $fbTotal -and $i -lt $fbResults.Count; $i++) {
                    $s = [string]$fbResults[$i].status
                    if ($s -cin @('added','exists') -or $script:FallbackText.ContainsKey($s)) { $fbStatuses[$i] = $s }
                }
            } catch { }
            $fbInput = $null; $fallbackEntries = @()
            $fbDone = 0
            for ($i = 0; $i -lt $fbTotal; $i++) {
                if ($fbStatuses[$i] -cin @('added','exists')) { $fbDone++ }
                else { Send-Event @{type='progress';message=('Запасной провайдер ' + ($i + 1) + ' не подключён: ' + $script:FallbackText[$fbStatuses[$i]] + '.')} }
            }
            $fallbackNote = "Запасные провайдеры: подключено $fbDone из $fbTotal."
            Send-Event @{type='progress';message=$fallbackNote}
        }
        # Optional Telegram bot. Same contract as the set: never terminal, one fixed
        # Russian line on failure, child output parsed (last line) and never relayed.
        # Voice input (local faster-whisper) needs the VC++ runtime: Telegram voice notes
        # AND the Desktop microphone, so this runs with or without a bot token. Before
        # the Telegram step, so a voice note works right after pairing. Present = silent
        # (the common case; UAC only on clean Windows); otherwise one start line + one
        # outcome line, never terminal.
        try {
            if (-not (Test-VcRuntime)) {
                Send-Event @{type='progress';message=$script:VcRuntimeText['start']}
                $vc = Install-VcRuntime
                if ($vc -eq 'present') { $vc = 'installed' }
                if (-not $script:VcRuntimeText.ContainsKey($vc) -or $vc -eq 'start') { $vc = 'failed' }
                Send-Event @{type='progress';message=$script:VcRuntimeText[$vc]}
            }
        } catch { Send-Event @{type='progress';message=$script:VcRuntimeText['failed']} }
        $telegramNote = ''; $telegramBot = $null; $tgAutostart = $false
        if ($telegramToken) {
            Send-Event @{type='progress';message='Подключаю вашего Телеграм-бота…'}
            $tgStatus = 'failed'; $tgBot = ''
            try {
                $script:ChildLabel = 'Подключение Телеграма'
                $tg = Run-Child $python @((Join-Path $PSScriptRoot 'telegram.py'),$homeDir,$repo) (@{telegram_bot_token=$telegramToken} | ConvertTo-Json -Compress) 600
                $tgLast = @($tg.Text -split "`n" | Where-Object { $_.Trim() })[-1]
                $tgReply = $tgLast | ConvertFrom-Json
                if ([string]$tgReply.status -cin @('connected','token','network','exists','open','saved','failed')) { $tgStatus = [string]$tgReply.status }
                if ($tgStatus -eq 'connected' -and ($tg.Code -ne 0 -or $tgReply.ok -ne $true)) { $tgStatus = 'failed' }
                if ($tgReply.bot -is [string] -and $tgReply.bot -cmatch '^[A-Za-z0-9_]{5,64}$') { $tgBot = ' @' + $tgReply.bot }
                if ($tgStatus -eq 'connected' -and [string]$tgReply.autostart -cin @('task','startup')) { $tgAutostart = $true }
            } catch { $tgStatus = 'failed' }
            $telegramToken = $null
            $tgText = @{
                'connected' = "Телеграм подключён. Напишите вашему боту$tgBot любое сообщение и нажмите «Проверить» на этом экране."
                'token'     = 'Телеграм не подключён: токен бота не принят. Hermes работает; проверьте токен у @BotFather и подключите бота позже в Hermes → «Сообщения».'
                'network'   = 'Телеграм не подключён: нет связи с серверами Телеграм. Hermes работает; включите VPN и подключите бота позже в Hermes → «Сообщения».'
                'exists'    = 'Телеграм не подключён: в Hermes уже настроен другой бот. Существующие настройки сохранены.'
                'open'      = 'Телеграм не подключён: в настройках Hermes бот открыт для всех. Настройки не изменены.'
                'saved'     = 'Телеграм-бот сохранён, но пока не запустился. Hermes работает; перезагрузите компьютер или откройте Hermes → «Сообщения» и перезапустите шлюз.'
                'failed'    = 'Телеграм не подключён из-за ошибки. Hermes работает; подключите бота позже в Hermes → «Сообщения».'
            }
            if ($tgStatus -ne 'connected') { Send-Event @{type='progress';message=$tgText[$tgStatus]} }
            $telegramNote = ' ' + $tgText[$tgStatus]
            if ($tgStatus -eq 'connected') {
                # The UI shows the owner-approval step only for a connected bot.
                $telegramBot = if ($tgBot) { $tgBot.Trim().TrimStart('@') } else { '-' }
                if (-not $tgAutostart) {
                    # gateway_windows.install may exit 0 without any login entry: say so, stay non-fatal.
                    $autostartLine = 'Бот работает сейчас, но автозапуск после перезагрузки не настроен: после перезагрузки откройте Hermes → «Сообщения» и запустите шлюз.'
                    Send-Event @{type='progress';message=$autostartLine}
                    $telegramNote += ' ' + $autostartLine
                }
            }
        }
        if ($fallbackNote) { $fallbackNote = ' ' + $fallbackNote }
        $successEvent = @{type='success';message=('Hermes ответил через ваш API; настройки сохранены, файлы Desktop проверены.' + $fallbackNote + $telegramNote);launch_path=$desktopExe;launch_args=@()}
        if ($telegramBot) { $successEvent['telegram_bot'] = $telegramBot }
        Send-Event $successEvent
        return 0
    } catch {
        $code = if ($_.Exception.Data['code']) { [string]$_.Exception.Data['code'] } else { 'INTERNAL' }
        $message = if ($_.Exception.Data['code']) { $_.Exception.Message } else { 'Не удалось завершить установку. Существующие данные не удалены; обратитесь в поддержку.' }
        Send-Event @{type='error';code=$code;message=$message}
        return 1
    } finally {
        $script:InstallState=$null
        $raw = $null; $inputData = $null; $telegramToken = $null; $fallbackEntries = $null; $fbInput = $null
        if ($locked) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}
# Dot-source permits offline unit tests of pure validation helpers, not fake installs.
if ($MyInvocation.InvocationName -ne '.') { exit (Main) }
