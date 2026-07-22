# One-time elevated setup for the keyboard app's Max Fan button.
#  1. Registers the Gigabyte ACPI-WMI fan class (so GB_WMIACPI_Set exists).
#  2. Creates two scheduled tasks that run the app in "--fan" mode with highest
#     privileges. The app triggers them via `schtasks /run`, so toggling the
#     fan never shows a UAC prompt, and because the app is a windowed process
#     there is no console flash.
# Run ONCE, elevated. The installer runs this automatically.

$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Register the fan WMI class from the clean-room MOF (idempotent).
$mof = Join-Path $app 'aero_fan.mof'
if (Test-Path $mof) { mofcomp $mof | Out-Null }
if (-not (Get-CimClass -Namespace root/WMI -ClassName GB_WMIACPI_Set -ErrorAction SilentlyContinue)) {
  throw "GB_WMIACPI_Set still missing after mofcomp - check aero_fan.mof is present in $app"
}

# 2. Work out how to launch the app: the frozen exe if installed, else pythonw
#    + the .pyw during development.
$exe = Join-Path $app 'KeyboardLight.exe'
if (Test-Path $exe) {
  $launch = $exe
  $prefix = ''
} else {
  $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
  if (-not $pyw) { throw "Neither KeyboardLight.exe nor pythonw.exe found." }
  $launch = $pyw
  $prefix = '"{0}" ' -f (Join-Path $app 'keyboard_light.pyw')
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$tasks = @(
  @{ Name = 'AeroFanMax';    Arg = '--fan max' },
  @{ Name = 'AeroFanNormal'; Arg = '--fan off' }
)
foreach ($t in $tasks) {
  $a = New-ScheduledTaskAction -Execute $launch -Argument ($prefix + $t.Arg)
  Register-ScheduledTask -TaskName $t.Name -Action $a -Principal $principal -Force | Out-Null
  Write-Host ("registered task {0}" -f $t.Name)
}
Write-Host "Fan setup complete. The Max Fan button is ready."
