# 1-stand-up.ps1 — поднимает сервер lite-kit для режима hotspot+ICS (Станция 1).
# HTTPS/DNS сервер отвечает колонке на 192.168.137.1, DNS слушает 127.0.0.1:53
# (ICS ретранслирует туда запросы колонки с 192.168.137.1).
# Запуск от имени администратора ПОСЛЕ 0-hotspot-up.ps1.

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot
$LiteKit = Join-Path $Root "lite-kit"
$StateDir = Join-Path $PSScriptRoot "state"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "[!] Запустите от имени администратора"; exit 1 }

$ApIp = "192.168.137.1"

# --- проверяем, что точка доступа поднята ---
$apAddr = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $ApIp }
if (-not $apAddr) {
    Write-Host "[!] $ApIp не найден ни на одном адаптере. Сначала запустите 0-hotspot-up.ps1"
    exit 1
}

# --- python ---
$py = $null
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) { $py = "py -3" }
if (-not $py) {
    $pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($pyExe) { $py = "`"$pyExe`"" }
}
if (-not $py) { Write-Host "[!] Python не найден. Установите с python.org (галочка Add to PATH)."; exit 1 }
Write-Host "[*] Python: $py"

# --- порты: 443/80 должны быть свободны на 0.0.0.0, 53 на 127.0.0.1 ---
$busy443 = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
if ($busy443) {
    $proc = Get-Process -Id $busy443[0].OwningProcess -ErrorAction SilentlyContinue
    Write-Host "[!] Порт 443 занят процессом $($proc.ProcessName) (PID $($proc.Id)): $($proc.Path)"
    Write-Host "    Серверу нужен порт 443 на $ApIp — освободите его на время настройки."
    exit 1
}
$busy80 = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue
if ($busy80) {
    $proc = Get-Process -Id $busy80[0].OwningProcess -ErrorAction SilentlyContinue
    Write-Host "[!] Порт 80 занят процессом $($proc.ProcessName) (PID $($proc.Id)). Освободите на время настройки."
    exit 1
}

# --- firewall ---
netsh advfirewall firewall delete rule name="lite-kit"       | Out-Null
netsh advfirewall firewall delete rule name="lite-kit dns"   | Out-Null
netsh advfirewall firewall add rule name="lite-kit" dir=in action=allow protocol=TCP localport=80,443 remoteip=192.168.137.0/24 profile=any | Out-Null
netsh advfirewall firewall add rule name="lite-kit dns" dir=in action=allow protocol=UDP localport=53 remoteip=192.168.137.0/24 profile=any | Out-Null

# --- стоп прошлого сервера ---
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*equasar_server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- запуск сервера ---
New-Item -ItemType Directory -Force -Path "$LiteKit\log" | Out-Null
$env:LOCAL_IP     = $ApIp          # что DNS отвечает на quasar.*
$env:BIND_IP      = $ApIp          # HTTP/HTTPS слушают только интерфейс точки доступа
$env:DNS_BIND_IP  = "127.0.0.1"    # DNS слушает localhost (туда ретранслирует ICS)
$env:BUILTIN_DNS  = "1"

$cmdLine = "/c cd /d `"$LiteKit`" && set LOCAL_IP=$ApIp&& set BIND_IP=$ApIp&& set DNS_BIND_IP=$ApIp&& set BUILTIN_DNS=1&& $py scripts\equasar_server.py --dns >> log\server.log 2>> log\server.err.log"
$null = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "cmd.exe $cmdLine"
    CurrentDirectory = $LiteKit
}
Start-Sleep -Seconds 3

$srv = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*equasar_server.py*" } | Select-Object -First 1
if (-not $srv) {
    Write-Host "[!] Сервер не запустился. Лог:"
    Get-Content "$LiteKit\log\server.err.log" -Tail 20 -ErrorAction SilentlyContinue
    Get-Content "$LiteKit\log\server.log" -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}
$srv.ProcessId | Out-File -Encoding ascii (Join-Path $StateDir "server.pid")
Write-Host "[*] Сервер lite-kit запущен (PID $($srv.ProcessId)), лог: lite-kit\log\server.log"

# --- проверка DNS-цепочки, как её увидит колонка ---
Write-Host ""
Write-Host "[*] Проверка: quasar.yandex.net через DNS точки доступа ($ApIp) — так спросит колонка..."
$r2 = nslookup quasar.yandex.net $ApIp 2>&1 | Out-String
if ($r2 -match [regex]::Escape($ApIp)) {
    Write-Host "    OK: цепочка колонка -> lite-kit работает ($ApIp)"
} else {
    Write-Host "[!] DNS не вернул $ApIp. Ответ:"
    Write-Host $r2
    Write-Host "    Проверьте, что DNS публичного адаптера = 192.168.137.1 (скрипт 0-hotspot-up.ps1)"
    Write-Host "    и что VPN отключён."
}

Write-Host ""
Write-Host "=== СТЕНД ГОТОВ ==="
Write-Host "  1. Сбросьте Станцию 1: выключить питание, зажать кнопку микрофона,"
Write-Host "     не отпуская вставить питание, держать 10-15 с до мигания."
Write-Host "  2. На телефоне ВЫКЛЮЧИТЕ Wi-Fi (оставьте мобильный интернет + Bluetooth)."
Write-Host "  3. 'Дом с Алисой' -> + -> 'Устройство с Алисой' -> до экрана выбора сети."
Write-Host "  4. Введите вручную сеть AlisaSetup и пароль -> 'Подключиться'."
Write-Host "  5. Смотрите лог: lite-kit\log\server.log — ждите SYNC ... adbd=on / adbEnabled=true"
Write-Host "  6. IP колонки: arp -a (ищите 192.168.137.x). Затем:"
Write-Host "     powershell -ExecutionPolicy Bypass -File scripts\2-flash-boot.ps1 -StationIP <IP>"
