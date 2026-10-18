[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$InkboardExe,[Parameter(Mandatory)][string]$ConfigDirectory,[string]$TaskName='Inkboard dashboard')
$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $InkboardExe).Path
$config = (Resolve-Path -LiteralPath $ConfigDirectory).Path
if ($exe.Contains('"') -or $config.Contains('"')) { throw 'Paths must not contain quote characters.' }
if (-not (Test-Path -LiteralPath (Join-Path $config 'config.json') -PathType Leaf)) { throw 'Initialize and check the setup first.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'A task with this name exists. Inspect it; it was not overwritten.' }
& $exe check --directory $config
if ($LASTEXITCODE -ne 0) { throw 'Offline configuration checks did not pass.' }
if ($PSCmdlet.ShouldProcess($TaskName,'Register a current-user login task')) {
  $action = New-ScheduledTaskAction -Execute $exe -Argument ('serve --directory "' + $config + '"')
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
  $settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
  Write-Host 'Registered an unprivileged task for this account. It starts after login, not before login. No firewall or remote-access rule was changed.'
}
