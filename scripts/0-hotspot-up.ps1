# 0-hotspot-up.ps1 — поднимает точку доступа на D-Link DWA-140 и включает ICS (NAT + DNS-релей).
# Запуск от имени администратора:  powershell -ExecutionPolicy Bypass -File .\0-hotspot-up.ps1
param(
    [string]$Ssid = "AlisaSetup",
    [string]$Key  = "alisa12345",
    # local    — DNS ведёт на lite-kit на этом ПК (127.0.0.1 для апстрима; рекомендуется)
    # external — DNS ведёт на внешний сервер разблокировки 78.24.223.83 (без lite-kit)
    [ValidateSet("local","external")] [string]$DnsMode = "local",
    # Имя адаптера с интернетом. По умолчанию определяется по маршруту 0.0.0.0/0.
    [string]$PublicAdapter = ""
)

$ErrorActionPreference = "Stop"
$StateDir = Join-Path $PSScriptRoot "state"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "[!] Запустите от имени администратора"; exit 1 }

# --- предупреждение про VPN ---
$vpn = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.Status -eq "Up" -and ($_.InterfaceDescription -match "AnyConnect|OpenVPN|TAP|WireGuard")
}
if ($vpn) {
    Write-Host "[!] ВНИМАНИЕ: активны VPN-адаптеры ($($vpn.Name -join ', '))."
    Write-Host "    VPN может перехватить DNS и сломать схему. Отключите VPN на время настройки."
    $ans = Read-Host "    Продолжить? (y/N)"
    if ($ans -ne "y") { exit 1 }
}

# --- определяем публичный адаптер (с интернетом) ---
if (-not $PublicAdapter) {
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
    if (-not $route) { Write-Host "[!] Нет маршрута по умолчанию — нет интернета?"; exit 1 }
    $PublicAdapter = (Get-NetAdapter -InterfaceIndex $route.ifIndex).Name
}
Write-Host "[*] Публичный адаптер (интернет): $PublicAdapter"

# --- сохраняем текущие DNS публичного адаптера для отката ---
$curDns = (Get-DnsClientServerAddress -InterfaceAlias $PublicAdapter -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses
[pscustomobject]@{ Adapter = $PublicAdapter; Dhcp = ($null -eq $curDns -or $curDns.Count -eq 0); Dns = ($curDns -join ",") } |
    ConvertTo-Json | Set-Content -Path (Join-Path $StateDir "dns-backup.json") -Encoding utf8
Write-Host "[*] Текущие DNS сохранены в state\dns-backup.json: $($curDns -join ', ')"

# --- поднимаем hostednetwork на DWA-140 ---
Write-Host "[*] Настраиваю hostednetwork: SSID=$Ssid"
netsh wlan set hostednetwork mode=allow ssid="$Ssid" key="$Key" | Out-Host
netsh wlan start hostednetwork | Out-Host

# --- ждём появления виртуального адаптера точки доступа ---
$hosted = $null
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $hosted = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
        $_.InterfaceDescription -match "Hosted Network|Wi-Fi Direct Virtual" -and $_.Status -eq "Up"
    } | Select-Object -First 1
    if ($hosted) { break }
}
if (-not $hosted) {
    Write-Host "[!] Виртуальный адаптер точки доступа не появился."
    Write-Host "    Проверьте: netsh wlan show hostednetwork; драйвер DWA-140."
    exit 1
}
Write-Host "[*] Адаптер точки доступа: $($hosted.Name) ($($hosted.InterfaceDescription))"

# --- включаем ICS: публичный адаптер делится на hosted ---
Write-Host "[*] Включаю Internet Connection Sharing ($PublicAdapter -> $($hosted.Name))..."
$scope = New-Object -ComObject HNetCfg.HNetShare
$conns = $scope.EnumEveryConnection
$pub = $null; $priv = $null
foreach ($c in $conns) {
    $props = $scope.NetConnectionProps($c)
    if ($props.Name -eq $PublicAdapter) { $pub = $c }
    if ($props.Name -eq $hosted.Name)  { $priv = $c }
}
if (-not $pub)  { Write-Host "[!] Не нашёл подключение '$PublicAdapter' в HNetShare"; exit 1 }
if (-not $priv) { Write-Host "[!] Не нашёл подключение '$($hosted.Name)' в HNetShare"; exit 1 }

$pubCfg  = $scope.INetSharingConfigurationForINetConnection($pub)
$privCfg = $scope.INetSharingConfigurationForINetConnection($priv)
try { if ($pubCfg.SharingEnabled)  { $pubCfg.DisableSharing() } } catch { Write-Host "[~] DisableSharing(public): $($_.Exception.Message)" }
try { if ($privCfg.SharingEnabled) { $privCfg.DisableSharing() } } catch { Write-Host "[~] DisableSharing(private): $($_.Exception.Message)" }
try {
    $pubCfg.EnableSharing(0)   # 0 = public (отдаёт интернет)
    Write-Host "[*] EnableSharing(public) OK"
} catch {
    Write-Host "[!] EnableSharing(public) ошибка: $($_.Exception.Message)"
    exit 1
}
try {
    $privCfg.EnableSharing(1)  # 1 = private (принимает, получает 192.168.137.1)
    Write-Host "[*] EnableSharing(private) OK"
} catch {
    Write-Host "[!] EnableSharing(private) ошибка: $($_.Exception.Message)"
    exit 1
}
Start-Sleep -Seconds 2
$ipCheck = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq "192.168.137.1" }
if (-not $ipCheck) { Write-Host "[!] 192.168.137.1 не появился на адаптере точки доступа"; exit 1 }
Write-Host "[*] ICS включён. Адрес точки доступа: 192.168.137.1 (DHCP+NAT от Windows)"

# --- DNS на публичном адаптере: куда ICS будет ретранслировать/раздавать запросы колонки ---
if ($DnsMode -eq "local") {
    Set-DnsClientServerAddress -InterfaceAlias $PublicAdapter -ServerAddresses @("192.168.137.1")
    Write-Host "[*] DNS апстрима = 192.168.137.1 (запросы колонки пойдут в lite-kit на этом ПК)"
} else {
    Set-DnsClientServerAddress -InterfaceAlias $PublicAdapter -ServerAddresses @("78.24.223.83")
    Write-Host "[*] DNS апстрима = 78.24.223.83 (внешний сервер разблокировки, lite-kit не нужен)"
}

Write-Host ""
Write-Host "=== ТОЧКА ДОСТУПА ПОДНЯТА ==="
Write-Host "  SSID    : $Ssid"
Write-Host "  Пароль  : $Key"
Write-Host "  Режим   : $DnsMode"
if ($DnsMode -eq "local") {
    Write-Host ""
    Write-Host "  Дальше: запустите 1-stand-up.ps1 (сервер lite-kit), затем подключайте колонку."
} else {
    Write-Host ""
    Write-Host "  Дальше: lite-kit не нужен. Сбрасывайте колонку и подключайте её к $Ssid,"
    Write-Host "  затем сразу scripts\2-flash-boot.ps1 -StationIP <IP колонки>."
}
