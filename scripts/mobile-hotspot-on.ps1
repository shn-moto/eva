# mobile-hotspot-on.ps1 — включает «Мобильный хот-спот» Windows (WinRT) с заданным SSID/паролем.
# Альтернатива netsh hostednetwork для старых драйверов.
param(
    [string]$Ssid = "AlisaSetup",
    [string]$Key  = "alisa12345"
)

[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
    $m = $asTaskGeneric.MakeGenericMethod($resultType)
    $task = $m.Invoke($null, @($op))
    $task.Wait() | Out-Null
    $task.Result
}

$connProfile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $connProfile) { Write-Host "[!] Нет интернет-подключения для раздачи"; exit 1 }
Write-Host "[*] Раздаём: $($connProfile.ProfileName)"

$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($connProfile)

# задаём свои SSID/пароль
$cfg = $mgr.GetCurrentAccessPointConfiguration()
$cfg.Ssid = $Ssid
$cfg.Passphrase = $Key
Await ($mgr.ConfigureAccessPointAsync($cfg)) ([object]) 2>$null | Out-Null

$res = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
Write-Host "[*] StartTetheringAsync: $($res.Status) $($res.AdditionalErrorMessage)"
if ($res.Status.ToString() -ne "Success") { exit 1 }

$cfg2 = $mgr.GetCurrentAccessPointConfiguration()
Write-Host ""
Write-Host "=== МОБИЛЬНЫЙ ХОТ-СПОТ ВКЛЮЧЁН ==="
Write-Host "  SSID   : $($cfg2.Ssid)"
Write-Host "  Пароль : $($cfg2.Passphrase)"
