# 9-teardown.ps1 — откат: стоп сервера, выключение ICS и точки доступа, возврат DNS.
# Запуск от имени администратора после завершения настройки колонки.

$ErrorActionPreference = "Continue"
$Root     = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $PSScriptRoot "state"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "[!] Запустите от имени администратора"; exit 1 }

# --- стоп сервера lite-kit ---
$srv = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*equasar_server.py*" }
if ($srv) {
    $srv | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "[*] Сервер lite-kit остановлен."
} else {
    Write-Host "[*] Сервер lite-kit не запущен."
}

# --- firewall ---
netsh advfirewall firewall delete rule name="lite-kit"     | Out-Null
netsh advfirewall firewall delete rule name="lite-kit dns" | Out-Null
Write-Host "[*] Правила firewall удалены."

# --- выключаем ICS ---
try {
    $scope = New-Object -ComObject HNetCfg.HNetShare
    foreach ($c in $scope.EnumEveryConnection) {
        $cfg = $scope.INetSharingConfigurationForINetConnection($c)
        if ($cfg.SharingEnabled) {
            $name = $scope.NetConnectionProps($c).Name
            $cfg.DisableSharing()
            Write-Host "[*] ICS выключен на '$name'."
        }
    }
} catch {
    Write-Host "[!] Не смог выключить ICS через COM: $($_.Exception.Message)"
}

# --- гасим hostednetwork ---
netsh wlan stop hostednetwork | Out-Host
netsh wlan set hostednetwork mode=disallow | Out-Host

# --- возвращаем DNS публичного адаптера ---
$bakFile = Join-Path $StateDir "dns-backup.json"
if (Test-Path $bakFile) {
    $bak = Get-Content $bakFile -Raw | ConvertFrom-Json
    if ($bak.Dhcp -or [string]::IsNullOrEmpty($bak.Dns)) {
        Set-DnsClientServerAddress -InterfaceAlias $bak.Adapter -ResetServerAddresses -ErrorAction SilentlyContinue
        Write-Host "[*] DNS на '$($bak.Adapter)' возвращён в режим DHCP."
    } else {
        Set-DnsClientServerAddress -InterfaceAlias $bak.Adapter -ServerAddresses ($bak.Dns -split ",") -ErrorAction SilentlyContinue
        Write-Host "[*] DNS на '$($bak.Adapter)' возвращён: $($bak.Dns)"
    }
} else {
    Write-Host "[!] state\dns-backup.json не найден — проверьте DNS публичного адаптера вручную."
}

Write-Host ""
Write-Host "=== ОТКАТ ВЫПОЛНЕН ==="
Write-Host "  Root и ADB на колонке сохранятся — они закреплены в boot-разделе."
Write-Host "  Бэкапы boot лежат в station1\backups\."
