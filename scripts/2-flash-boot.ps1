# 2-flash-boot.ps1 — Станция 1: бэкап boot, запись boot-root.img, проверка SHA-256, перезагрузка.
# Повторяет вручную то, что делает AliceBridge. Запуск ПОСЛЕ того, как adb доступен.
# Пример: powershell -ExecutionPolicy Bypass -File .\2-flash-boot.ps1 -StationIP 192.168.137.45
param(
    [Parameter(Mandatory=$true)][string]$StationIP,
    [switch]$BackupOnly   # только снять бэкап boot, ничего не записывать
)

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$Adb   = Join-Path $Root "tools\platform-tools\adb.exe"
$Img   = Join-Path $Root "station1\boot-root.img"
$BakDir = Join-Path $Root "station1\backups"
New-Item -ItemType Directory -Force -Path $BakDir | Out-Null

$BootDev = "/dev/block/mmcblk0p6"   # раздел boot (16 МБ) на tulip
$ExpectedSize = 16777216            # 16 МБ — размер boot-root.img и раздела

if (-not (Test-Path $Adb)) { Write-Host "[!] adb не найден: $Adb"; exit 1 }
if (-not (Test-Path $Img)) { Write-Host "[!] нет образа: $Img"; exit 1 }

$localHash = (Get-FileHash $Img -Algorithm SHA256).Hash.ToLower()
Write-Host "[*] boot-root.img SHA256: $localHash"

# --- подключение ---
& $Adb disconnect "$StationIP`:5555" | Out-Null
$conn = & $Adb connect "$StationIP`:5555" 2>&1 | Out-String
Write-Host "[*] $conn"
$id = & $Adb -s "$StationIP`:5555" shell "su 0 id" 2>&1 | Out-String
if ($id -notmatch "uid=0") {
    Write-Host "[!] ADB без root: $id"
    Write-Host "    Дождитесь adbd=on/adbEnabled=true в логе lite-kit и передёрните питание колонки."
    exit 1
}
Write-Host "[*] ADB root подтверждён: $($id.Trim())"

# --- проверка модели ---
$platform = (& $Adb -s "$StationIP`:5555" shell getprop ro.board.platform 2>&1 | Out-String).Trim()
Write-Host "[*] ro.board.platform = $platform"
if ($platform -ne "tulip") {
    Write-Host "[!] Это НЕ Станция 1 (tulip). Образ применять НЕЛЬЗЯ. Прерываю."
    exit 1
}

# --- бэкап текущего boot ---
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$bakRemote = "/data/local/tmp/boot-orig.img"
$bakLocal  = Join-Path $BakDir "boot-orig-$ts.img"
Write-Host "[*] Снимаю бэкап boot -> $bakLocal"
& $Adb -s "$StationIP`:5555" shell "su 0 dd if=$BootDev bs=1048576 | cat > $bakRemote" 2>&1 | Out-Null
& $Adb -s "$StationIP`:5555" pull $bakRemote $bakLocal | Out-Host
$bakHash = (Get-FileHash $bakLocal -Algorithm SHA256).Hash.ToLower()
$bakSize = (Get-Item $bakLocal).Length
if ($bakSize -ne $ExpectedSize) { Write-Host "[!] Бэкап странного размера: $bakSize (ожидалось $ExpectedSize). Прерываю."; exit 1 }
Write-Host "[*] Бэкап сохранён, SHA256: $bakHash"

if ($BackupOnly) { Write-Host "[*] Режим -BackupOnly: запись не выполняется."; exit 0 }

# --- закачка образа и контроль целостности ---
Write-Host "[*] Загружаю boot-root.img на колонку..."
& $Adb -s "$StationIP`:5555" push $Img /data/local/tmp/boot-root.img | Out-Host
& $Adb -s "$StationIP`:5555" pull /data/local/tmp/boot-root.img "$env:TEMP\boot-root-check.img" | Out-Null
$upHash = (Get-FileHash "$env:TEMP\boot-root-check.img" -Algorithm SHA256).Hash.ToLower()
Remove-Item "$env:TEMP\boot-root-check.img" -Force -ErrorAction SilentlyContinue
if ($upHash -ne $localHash) { Write-Host "[!] Образ повредился при передаче ($upHash). Прерываю."; exit 1 }
Write-Host "[*] Целостность переданного образа подтверждена."

# --- запись в раздел boot ---
Write-Host "[*] Пишу boot-root.img в $BootDev ..."
& $Adb -s "$StationIP`:5555" shell "cat /data/local/tmp/boot-root.img | su 0 dd of=$BootDev bs=4096; sync" 2>&1 | Out-Host

# --- сверка записанного раздела ---
Write-Host "[*] Считываю раздел обратно и сверяю SHA-256..."
& $Adb -s "$StationIP`:5555" shell "su 0 dd if=$BootDev bs=1048576 | cat > /data/local/tmp/boot-verify.img; sync" 2>&1 | Out-Null
& $Adb -s "$StationIP`:5555" pull /data/local/tmp/boot-verify.img "$env:TEMP\boot-verify.img" | Out-Null
$wrHash = (Get-FileHash "$env:TEMP\boot-verify.img" -Algorithm SHA256).Hash.ToLower()
Remove-Item "$env:TEMP\boot-verify.img" -Force -ErrorAction SilentlyContinue

if ($wrHash -ne $localHash) {
    Write-Host "[!] ЗАПИСЬ НЕ СОВПАЛА ($wrHash). Восстанавливаю исходный boot..."
    & $Adb -s "$StationIP`:5555" shell "cat $bakRemote | su 0 dd of=$BootDev bs=4096; sync" 2>&1 | Out-Host
    Write-Host "[!] Исходный boot возвращён. Разбирайтесь с ошибкой, колонка не перезагружена."
    exit 1
}
Write-Host "[*] Раздел записан и совпал с образом."

# --- уборка и перезагрузка ---
& $Adb -s "$StationIP`:5555" shell "rm -f /data/local/tmp/boot-root.img /data/local/tmp/boot-verify.img; sync" | Out-Null
Write-Host "[*] Перезагружаю колонку (бэкап оставлен в $bakRemote и на ПК)..."
& $Adb -s "$StationIP`:5555" reboot

Write-Host ""
Write-Host "=== BOOT ЗАПИСАН ==="
Write-Host "  Подождите 1-2 минуты, затем проверьте:"
Write-Host "  $Adb connect $StationIP`:5555"
Write-Host "  $Adb -s $StationIP`:5555 shell id      # должно быть uid=0(root)"
Write-Host ""
Write-Host "  Дальше по плану: SSH (порт 22), отключение обновлений и установка Debian —"
Write-Host "  см. README.md, шаги 3 и 4."
Write-Host "  Откат boot: adb push station1\backups\boot-orig-$ts.img + dd в $BootDev (как выше)."
