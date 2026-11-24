# Eva — домашний голосовой ассистент на Яндекс Станции 1

Своя система на Станции 1 (Debian, все 7 микрофонов, экран на ТВ через HDMI) и
тяжёлые сервисы на домашнем сервере с GPU: распознавание речи (GigaAM),
локальный роутер команд (Qwen на llama.cpp), музыка; умные ответы — Claude.

| папка | что |
|---|---|
| `app/` | мозг и ухо на Станции: диалог, роутер, навыки, речь, микрофоны |
| `ui/spike/` | экран на ТВ (cog/WPE): лицо, Live2D, плеер, графики; ui_server |
| `os/` | своя система для Станции: загрузка, rootfs, аудио-инициализация |
| `services/` | сервисы на сервере: STT (`stt`), музыка (`music`), индексатор памяти, старый CPU-llama |
| `station1/` | набор для получения root на Станции 1 (см. ниже) |
| `legacy/` | старый ассистент и разовые скрипты времён рутования — для справки |
| `deploy/` | деплой из git на Станцию и на сервер |
| `eva.example/` | пример профиля дома (сам профиль приватный) |
| `docs/` | архитектура и планы |

## Профиль дома — всё личное вне кода

Всё, что относится к конкретной семье, лежит в `/opt/eva/eva/` на Станции и **в
git не попадает**: персона (`BOOTSTRAP.md`, `LEGEND.md`, `PRIVATE.md`), формы
имён (`names.json`) и `profile.json` — дети, имена для распознавания, голоса,
пользователи финансов, адреса сервисов (`services.hub`). Формат — в
`eva.example/`. Ключи API — в `/etc/eva/secrets.env` на устройстве.

## Деплой — только из git

Деплоится коммит, который уже есть на `origin`, а не рабочая папка:

```
deploy/station.sh --diff     # что изменится на Станции
deploy/station.sh            # origin/main -> /opt/eva/{app,ui/spike}, перезапуск затронутых служб
deploy/linux.sh stt          # сервис из коммита: сборка образа <имя>:<коммит> и пересоздание контейнера
deploy/linux.sh music | router | gpu-arbiter | admin
```

На Станции коммит записан в `/opt/eva/DEPLOYED`, на сервере — в `~/eva-src/<сервис>/DEPLOYED`.

**Админка** (`services/admin`, порт 8908, только из домашней сети): фразы из памяти по дням со
звуком (STT хранит клип фразы 30 дней, `STT_LOG_DAYS`), «оставить навсегда» (копия в
`~/eva-data/stt-keep`), удаление вместе со звуком и из поиска по памяти, поиск по тексту, служебные
логи вокруг фразы или за день. На сервере нужен `~/eva-data/admin.env` с `ASSISTANT_PG_DSN`, а в
`utterances` — колонки `audio text` и `keep boolean` (их добавляет владелец таблицы, см. `app/memory.py`).
Код в контейнеры через `docker cp` не кладём: он теряется при пересоздании.

---

# Получение root на Станции 1 (исходная методика)

Рабочий комплект для получения root на **Яндекс Станции 1 (YNDX-0001, плата `tulip`)**
через точку доступа на USB-адаптере **D-Link DWA-140** и установки Debian.

Источники методики: [hackalice.me](https://hackalice.me) (root, AliceBridge, Debian на Станции 1),
репозитории [lite-kit](https://github.com/krevetkqqq/lite-kit) и
[yandex-station-linux](https://github.com/MyszkinPL/yandex-station-linux).

## ⚠️ Важная поправка к исходному плану

Станция 1 — **Android-платформа** (Allwinner R18), а не Amlogic+Buildroot, как младшие
модели. Поэтому классический DNS-метод с сервером `179.254.167.126` на ней root **не даёт**.
Рабочая цепочка для Станции 1 (как в приложении AliceBridge, только с ПК):

1. DNS подменяет `quasar.yandex.net` → колонка получает конфиг с включённым ADB;
2. по ADB в раздел `boot` (`mmcblk0p6`) записывается **boot-root.img** → постоянный root,
   переживающий сбросы DNS и перезагрузки;
3. дальше — отключение обновлений и установка **Debian 13** одной командой.

## Топология

```
Интернет ── Ethernet 3 (Intel I219-V) ── Этот ПК ── DWA-140 (hostednetwork "AlisaSetup")
                                              │           192.168.137.1 (ICS: DHCP+NAT+DNS-релей)
                                              │                     │
                                              │              Станция 1 (Wi-Fi клиент)
                                              │
                              lite-kit: DNS и HTTPS на 192.168.137.1 (53/80/443)
                              (DNS публичного адаптера = 192.168.137.1 → quasar.* → точка доступа)
```

## Состав комплекта

| Путь | Что это |
|---|---|
| `lite-kit/` | Локальный сервер разблокировки (DNS+HTTPS), пропатчен `DNS_BIND_IP` |
| `station1/boot-root.img` | Boot-образ с root для Станции 1, SHA256 `5bb6c43f…959478` |
| `station1/boot-factory.img` | Заводской boot (для отката), SHA256 `e1dbdbb6…d8ad8dd` |
| `station1/station1-freeze-updatesd.sh` | Заморозка службы обновлений |
| `tools/platform-tools/` | adb.exe для Windows |
| `scripts/0-hotspot-up.ps1` | Поднять точку доступа на DWA-140 + ICS + DNS |
| `scripts/1-stand-up.ps1` | Запустить lite-kit в этой топологии |
| `scripts/2-flash-boot.ps1` | Бэкап boot → запись boot-root.img → сверка SHA-256 → reboot |
| `scripts/9-teardown.ps1` | Полный откат сетевых изменений |

## Подготовка (важно)

- **Порт 443 на этом ПК занят процессом `java` (C:\Java\…)** — серверу нужен 443.
  На время настройки остановите это приложение (или оно будет мешать колонке
  достучаться до HTTPS). Скрипт `1-stand-up.ps1` проверит и напомнит.
- **Отключите VPN** (на ПК есть Cisco AnyConnect и OpenVPN-адаптеры) — VPN перехватит DNS.
- Телефон с приложением «Дом с Алисой», Bluetooth + геолокация, **Wi-Fi на телефоне
  выключить** (оставить мобильный интернет), сеть колонке вводить вручную.
- Скрипты запускать **от имени администратора**:
  `powershell -ExecutionPolicy Bypass -File scripts\<имя>.ps1`

## Шаг 1 — root

### 1.1 Точка доступа на DWA-140

```powershell
powershell -ExecutionPolicy Bypass -File scripts\0-hotspot-up.ps1
```

Создаёт сеть `AlisaSetup` / `alisa12345`, включает ICS (Ethernet 3 → точка доступа),
DNS апстрима ставит `192.168.137.1` (запросы колонки пойдут в локальный lite-kit).
Вариант без своего сервера: `… -DnsMode external` — DNS сразу на `78.24.223.83`
(сервер AliceBridge), тогда шаг 1.2 пропускается, но логов и контроля не будет.

### 1.2 Сервер разблокировки

```powershell
powershell -ExecutionPolicy Bypass -File scripts\1-stand-up.ps1
```

Поднимает lite-kit, проверяет всю DNS-цепочку так, как её увидит колонка
(`nslookup quasar.yandex.net 192.168.137.1` должен вернуть `192.168.137.1`).

### 1.3 Колонка

1. Сброс: выдернуть питание → зажать кнопку **отключения микрофона** →
   не отпуская вставить питание → держать 10–15 с до мигания и фразы Алисы.
2. «Дом с Алисой» → «+» → «Устройство с Алисой» → дойти до экрана Wi-Fi.
3. Ввести вручную `AlisaSetup` + пароль → «Подключиться».
4. В `lite-kit\log\server.log` ждать строку `SYNC … adbd=on` / `adbEnabled=true`.
5. IP колонки узнать через `arp -a` (диапазон `192.168.137.x`).

### 1.4 Прошивка boot-root.img

```powershell
# сначала можно только бэкап:
powershell -ExecutionPolicy Bypass -File scripts\2-flash-boot.ps1 -StationIP <IP> -BackupOnly
# затем запись:
powershell -ExecutionPolicy Bypass -File scripts\2-flash-boot.ps1 -StationIP <IP>
```

Скрипт проверяет `uid=0`, модель `tulip`, снимает бэкап boot в `station1\backups\`,
пишет образ, **сверяет SHA-256 записанного раздела** и перезагружает колонку.
После перезагрузки `adb shell id` → `uid=0(root)` — постоянный root получен,
точку доступа и сервер можно свернуть (`scripts\9-teardown.ps1`) или оставить до конца настройки.

## Шаг 2 — установка Linux (Debian 13)

Из root-shell Android (по adb):

```
tools\platform-tools\adb.exe connect <IP>:5555
tools\platform-tools\adb.exe shell
curl -kfsSL https://github.com/MyszkinPL/yandex-station-linux/releases/latest/download/install.sh | sh
```

Установщик сам проверит железо, предложит режим загрузки
(«Debian через Android» — надёжно; «Debian сразу» — SSH через ~15 с после включения),
сгенерирует пароль root и перезагрузит колонку. Удаление (`uninstall`) возвращает сток.

## Шаг 3 — отключение обновлений (рекомендуется)

Если Алиса уходит в цикл обязательного обновления — рецепт
[station1-disable-updates](https://hackalice.me/recipes/station1-disable-updates/):
скрипт `station1\station1-freeze-updatesd.sh` кладётся на колонку и стартует из
`/data/dropbear/start.sh` (появляется после установки SSH/dropbear; см. рецепт).

## Откат

- Сеть на ПК: `scripts\9-teardown.ps1` — стоп сервера, выключение ICS/точки доступа,
  возврат DNS из `scripts\state\dns-backup.json`.
- Boot колонки: бэкапы в `station1\backups\`; записать обратно через adb `dd`
  (та же процедура, что в `2-flash-boot.ps1`, источник — бэкап).
- Debian: `sh /data/local/tmp/station-debian.run uninstall` из Android.

## Риски

- Сброс колонки отвязывает аккаунт и чистит `/data`.
- Модификация boot может «окирпичить» колонку — бэкап делается до записи,
  заводской образ лежит в `station1\boot-factory.img`.
- Root открывает ADB без пароля в вашей сети — не пробрасывайте порт 5555 наружу.
