$ErrorActionPreference = 'Stop'
$dir = "C:\Users\kevinyan\Documents\Codex\2026-05-13\https-github-com-gatheringtech001-copilot-4-claude-code"
$taskName = 'CopilotProxy8082'
$log = Join-Path $dir 'logs\watchdog.log'

function Write-WatchdogLog([string]$message) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $message" |
        Add-Content -LiteralPath $log
}

try {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $expectedPython = Join-Path $dir '.venv\Scripts\pythonw.exe'
    if ($task.Actions.Count -ne 1 -or
        $task.Actions[0].Execute -ne $expectedPython -or
        $task.Actions[0].WorkingDirectory -ne $dir -or
        $task.Actions[0].Arguments -ne 'run-cp4cc.pyw --port 8082') {
        throw 'Service task identity mismatch; refusing to restart.'
    }

    $listener = Get-NetTCPConnection -State Listen -LocalPort 8082 -ErrorAction SilentlyContinue
    if ($listener) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)"
        if ($parent.ExecutablePath -ne $expectedPython -or
            $process.CommandLine -notmatch 'cp4cc\.py.*--port 8082' -or
            $task.State -ne 'Running') {
            throw 'Port 8082 belongs to an unmanaged process; refusing to stop it.'
        }
    }

    if ($task.State -eq 'Running') {
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8082/health' -TimeoutSec 10
                if ($health.status -eq 'ok') { exit 0 }
                Write-WatchdogLog 'Health endpoint returned a non-ok status.'
            } catch {
                Write-WatchdogLog "Health check failed: $($_.Exception.Message)"
            }
            Start-Sleep -Seconds 5
        }
        Write-WatchdogLog 'Health checks exhausted; restarting managed service.'
        Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Start-Sleep -Seconds 3
    }

    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        Start-Sleep -Seconds 5
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8082/health' -TimeoutSec 5
            if ($health.status -eq 'ok') {
                Write-WatchdogLog 'Managed service started and healthy on port 8082.'
                exit 0
            }
        } catch {
            Write-WatchdogLog "Startup health check failed: $($_.Exception.Message)"
        }
    }
    throw 'Managed service failed to become healthy.'
} catch {
    Write-WatchdogLog "ERROR: $($_.Exception.Message)"
    throw
}
