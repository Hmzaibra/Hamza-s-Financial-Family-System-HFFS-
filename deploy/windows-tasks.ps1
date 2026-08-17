# Scheduled jobs for the laptop, until the Pi takes over.
#
# The Linux box gets these from cron (deploy/crontab.example). Windows has no
# cron, so the same four jobs are registered with Task Scheduler here. Run this
# once, from an **elevated** PowerShell:
#
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\deploy\windows-tasks.ps1
#
# To see them afterwards: Task Scheduler -> Task Scheduler Library -> Expenses.
# To remove them all:  .\deploy\windows-tasks.ps1 -Remove
#
# Two differences from cron that are worth knowing before something looks
# broken:
#
#   * a laptop is asleep at 04:00, so every task here is registered with
#     StartWhenAvailable, which runs a missed job the next time the machine is
#     awake. Cron on a Pi that is always on does not need this and does not do
#     it.
#   * the tasks run whether or not anyone is signed in, but only on battery if
#     you say so — the default is to stop on battery, which on a laptop that
#     lives unplugged means nothing ever runs. Overridden below.

param(
  [string]$AppDir = (Split-Path -Parent $PSScriptRoot),
  [switch]$Remove
)

$folder = "\Expenses"
$python = Join-Path $AppDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  throw "No virtualenv at $python — create it first (see DEPLOY.md)."
}

$jobs = @(
  @{ Name = "Expenses fetch-rates"
     Args = "-m flask --app app fetch-rates"
     Trigger = (New-ScheduledTaskTrigger -Daily -At 4:10am)
     Why = "exchange rates; also what wakes the tenfold-disagreement guard on transfers" },

  @{ Name = "Expenses check-limits"
     Args = "-m flask --app app check-limits"
     Trigger = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(1) `
                 -RepetitionInterval (New-TimeSpan -Hours 1))
     Why = "budget warnings; at most one message per threshold per period" },

  @{ Name = "Expenses sweep-uploads"
     Args = "-m flask --app app sweep-uploads"
     Trigger = (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:30am)
     Why = "deletes receipt files whose rows are already gone" },

  @{ Name = "Expenses backup"
     Args = "scripts\backup.py --keep 14"
     Trigger = (New-ScheduledTaskTrigger -Daily -At 2:00am)
     Why = "app.db via SQLite's backup API, plus a tarball of uploads/" }
)

if ($Remove) {
  foreach ($job in $jobs) {
    Unregister-ScheduledTask -TaskName $job.Name -TaskPath $folder -Confirm:$false `
      -ErrorAction SilentlyContinue
    Write-Host "removed $($job.Name)"
  }
  return
}

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -DontStopIfGoingOnBatteries `
  -AllowStartIfOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
  -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U `
  -RunLevel Limited

foreach ($job in $jobs) {
  $action = New-ScheduledTaskAction -Execute $python -Argument $job.Args `
    -WorkingDirectory $AppDir
  Register-ScheduledTask -TaskName $job.Name -TaskPath $folder -Action $action `
    -Trigger $job.Trigger -Settings $settings -Principal $principal -Force | Out-Null
  Write-Host "registered $($job.Name) — $($job.Why)"
}

Write-Host ""
Write-Host "Run one now to check it works:"
Write-Host "  Start-ScheduledTask -TaskPath '$folder' -TaskName 'Expenses fetch-rates'"
Write-Host "Then look at the rates it cached:"
Write-Host "  .venv\Scripts\python.exe -c ""import sqlite3;print(sqlite3.connect('app.db').execute('select count(*), max(fetched_at) from fx_rates').fetchone())"""
