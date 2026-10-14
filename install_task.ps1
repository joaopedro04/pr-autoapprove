param(
    [switch]$Uninstall,
    [int]$IntervalMinutes = 0
)

$ErrorActionPreference = 'Stop'
$botDir = $PSScriptRoot

$envVars = @{}
$envFile = Join-Path $botDir '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content -Encoding UTF8 $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $envVars[$Matches[1]] = $Matches[2].Trim('"', "'")
        }
    }
}

$taskName = if ($envVars['SCHEDULE_TASK_NAME']) { $envVars['SCHEDULE_TASK_NAME'] } else { 'pr-autoapprove' }
$dashTask = "$taskName-dashboard"
$port = if ($envVars['DASHBOARD_PORT']) { $envVars['DASHBOARD_PORT'] } else { '8765' }

if ($Uninstall) {
    foreach ($name in @($taskName, $dashTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Tarefa $name removida."
        }
    }
    return
}

if ($IntervalMinutes -le 0) {
    $IntervalMinutes = if ($envVars['SCHEDULE_INTERVAL_MINUTES']) { [int]$envVars['SCHEDULE_INTERVAL_MINUTES'] } else { 5 }
}

$pythonw = $envVars['PYTHONW_PATH']
if (-not $pythonw) { $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
if (-not $pythonw) { $pythonw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe' }
if (-not (Test-Path $pythonw)) { throw "pythonw.exe não encontrado; defina PYTHONW_PATH no .env" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$botDir\pr_bot.py`"" -WorkingDirectory $botDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Revisa PRs com uma skill do Claude Code e aprova quando os threads do bot estão resolvidos e o CI está verde.' `
    -Force | Out-Null

Stop-ScheduledTask -TaskName $dashTask -ErrorAction SilentlyContinue
$dashAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$botDir\dashboard.py`"" -WorkingDirectory $botDir
$dashTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$dashSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $dashTask -Action $dashAction -Trigger $dashTrigger -Settings $dashSettings -Principal $principal `
    -Description 'Painel ao vivo do pr-autoapprove em 127.0.0.1.' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $dashTask

Write-Host "Tarefa $taskName a cada $IntervalMinutes min, com $pythonw"
Write-Host "Painel ao vivo ($dashTask): http://127.0.0.1:$port/"
Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
