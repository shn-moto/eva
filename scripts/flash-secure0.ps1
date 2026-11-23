$A = 'D:\PRG\Pyton\new_alisa\tools\platform-tools\adb.exe'
$S = '192.168.137.116:5555'
& $A -s $S push 'D:\PRG\Pyton\new_alisa\station1\boot-secure0.img' /data/local/tmp/boot-secure0.img
if ($LASTEXITCODE -ne 0) { Write-Host '[!] push failed'; exit 1 }
& $A -s $S shell 'cat /data/local/tmp/boot-secure0.img | su 0 dd of=/dev/block/mmcblk0p6 bs=4096; sync'
& $A -s $S shell 'su 0 dd if=/dev/block/mmcblk0p6 bs=1048576 | cat > /data/local/tmp/boot-verify3.img; sync'
& $A -s $S pull /data/local/tmp/boot-verify3.img 'D:\PRG\Pyton\new_alisa\station1\work\boot-verify3.img'
$h1 = (Get-FileHash 'D:\PRG\Pyton\new_alisa\station1\work\boot-verify3.img' -Algorithm SHA256).Hash
$h2 = (Get-FileHash 'D:\PRG\Pyton\new_alisa\station1\boot-secure0.img' -Algorithm SHA256).Hash
Write-Host "verify: $h1"
Write-Host "expect: $h2"
if ($h1 -eq $h2) {
    Write-Host '[*] ЗАПИСЬ ПОДТВЕРЖДЕНА. Перезагружаю...'
    & $A -s $S shell 'rm -f /data/local/tmp/boot-secure0.img /data/local/tmp/boot-verify3.img'
    & $A -s $S reboot
} else {
    Write-Host '[!] НЕ СОВПАЛО — колонку не трогаю, разбирайтесь.'
    exit 1
}
