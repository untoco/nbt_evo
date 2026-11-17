# Окружение и инструменты

Версия: 2026-11-17. Инструкция описывает существующие команды; результаты их проверок приведены в [фактах](facts.md).

## Развёртывание через Ansible

На подготовленном Mac Apple Silicon нужны работающий Docker, Ansible, Python 3.11+, Git и curl. Команды запускаются из корня репозитория:

```sh
./tools/nbt-env setup
```

[Playbook](../automation/ansible/environment.yml) выполняет полный цикл:

1. Проверяет инструменты и доступность Docker, создаёт рабочие каталоги.
2. Создаёт `config/nbt/environment.local.yml` из [шаблона](../config/nbt/environment.example.yml), сохраняя существующие настройки.
3. Получает закреплённую ревизию QNX-комплекта из публичного репозитория, если локального дерева ещё нет, и сверяет контрольные файлы по [lock-файлу](../tools/nbt-dev/toolchain.lock.json).
4. Загружает Debian-пакеты и подписанные индексы, GNU-исходники, Ghidra, Node и REA; проверяет предусмотренные манифестами хеши. Пакеты общей среды устанавливаются внутри её Docker-образа.
5. Собирает недостающие образы; существующие использует повторно. Пересборка включается параметром `nbt_rebuild: true`.
6. Запускает контейнеры проверки: общие инструменты, компиляцию C/C++98, Ghidra doctor и декомпиляцию тестового QNX ELF с проверкой неизменности входного файла.
7. Создаёт конфигурацию MCP и записывает результат в `.build/bootstrap/result.json`.

Контейнеры запускаются для конкретной команды и удаляются после завершения. Постоянные сервисы для этой среды не требуются. Playbook не устанавливает пакеты в macOS, не меняет настройки Colima и не подключается к автомобилю.

Другие команды:

```sh
./tools/nbt-env plan
./tools/nbt-env check
```

plan только показывает задачи. check проверяет уже подготовленные образы без загрузки и сборки; служебные каталоги и локальные конфиги создаются при необходимости. Можно вызвать Ansible напрямую: `ansible-playbook -i localhost, automation/ansible/environment.yml`.

Параметры задаются в локальном YAML: `nbt_components` выбирает dev/qnx/re, `nbt_offline` запрещает подготовку через интернет, `nbt_rebuild` включает пересборку. В офлайн-режиме нужны заранее загруженные образы; отсутствие образа завершает playbook ошибкой. Существующий `re.local.json` сохраняется и управляет параметрами REA.

Полный журнал находится в `.build/bootstrap/latest.log`. Успешный запуск заканчивается сообщением `Environment verified` и `failed=0` в `PLAY RECAP`. При ошибке запуск прекращается, старый результат успеха удаляется. После исправления можно повторить ту же команду; корректные загрузки и существующие образы используются повторно. Первая сборка QNX может занимать десятки минут.

## Где что работает

| Компонент | Место запуска | Назначение |
|---|---|---|
| Python 3.11+, системные SSH и сетевые утилиты | macOS | Конфигурация сеанса, ENET, чтение/копирование через SSH |
| nbt-evo-dev:2026-11-14 | Docker, Linux AMD64 | Общие инструменты сборки и анализа |
| nbt-evo-qnx-armv7:8.5-arm64-2026-11-14 | Docker, Linux ARM64 | GCC 8.5, сборка ARM32 QNX |
| nbt-evo-re:ghidra12.2-rea6.9.0 | Docker, Linux ARM64 | Ghidra/REA; версии и хеши зафиксированы в artifacts.lock.json |

ARM64 здесь описывает платформу компилятора; выходной QNX ELF имеет архитектуру ARM32. Совместимость с C++ ABI BMW HMI этими сборками не проверена.

Нужны уже работающий Docker, Git, Python и curl. При использовании Colima каталог проекта должен быть доступен виртуальной машине. SDK и библиотеки HU в публичный clone не входят. На Mac не требуется устанавливать Java, Node или компилятор QNX для запуска этих контейнеров.

## Ghidra и REA

Из корня проекта:

```sh
python3 tools/nbt_re.py init
python3 tools/nbt_re.py prepare
python3 tools/nbt_re.py build
python3 tools/nbt_re.py doctor
```

init создаёт локальный `config/nbt/re.local.json` из [примера](../config/nbt/re.example.json). prepare загружает архивы и проверяет хеши из [artifacts.lock.json](../tools/nbt-re/artifacts.lock.json). build собирает образ по [Dockerfile](../tools/nbt-re/Dockerfile); подготовка и сборка требуют интернета. Анализ в подготовленном образе запускается с `--network none --pull never`.

Загрузчик сохраняет `.part` и продолжает его после обрыва. По умолчанию одна попытка ограничена четырьмя часами, выполняется до пяти попыток; отсутствие передачи данных в течение 90 секунд вызывает повтор. Настройки `download_timeout_seconds`, `download_attempts` и `download_stall_seconds` задаются в `re.local.json`. В старых конфигах без этих полей используются новые значения по умолчанию. Полностью докачанный другим клиентом файл `.part` принимается после проверки закреплённого хеша. Ошибка HTTP/таймаут не считается доказательством несовпадения хеша. Не запускать несколько загрузчиков на один файл одновременно.

Пример для имеющегося тестового ELF в локальном каталоге:

```sh
python3 tools/nbt_re.py analyze .build/nbt-re/inputs/fixture-qnx-arm32
python3 tools/nbt_re.py decompile .build/nbt-re/inputs/fixture-qnx-arm32 hu_scale_rpm
python3 tools/nbt_re.py mcp-config
```

ELF отсутствует в Git; его исходник — [fixture.c](../tools/nbt-re/fixture.c). Вместо этого пути можно передать собственный существующий бинарный файл. Анализируемый файл подключается только для чтения. Отчёты и состояние сохраняются в `.build/nbt-re/`; mcp-config создаёт `.build/nbt-re/mcp.json`, не меняя настройки клиента автоматически.

## QNX и общая среда

```sh
tools/nbt-dev/run build
tools/nbt-dev/run doctor
tools/nbt-dev/qnx-run check
```

run build собирает общую среду из [Dockerfile](../tools/nbt-dev/Dockerfile). Её doctor отдельно сообщает об отсутствии неподключённых SDK и frameworks.

qnx-run check использует уже подготовленный QNX-образ, компилирует [C](../tools/nbt-dev/examples/hello-qnx.c) и [C++98](../tools/nbt-dev/examples/hello-qnx.cpp) и проверяет ELF. Ожидаемый итог — ELF32 ARM, interpreter `/usr/lib/ldqnx.so.2`, отчёты и SHA-256 в `.build/nbt-qnx/out/`. Команда не запускает программы на автомобиле.

Для пересборки QNX-образа нужны локальные `tools/qnx/`, исходные GNU-архивы и Debian-пакеты. Проверка и сборка:

```sh
python3 tools/nbt-dev/qnx-deps.py check
tools/nbt-dev/qnx-run build
```

[Манифест зависимостей](../tools/nbt-dev/dependencies-arm64.json) и [подготовка сборки](../tools/nbt-dev/prepare-qnx-build.py) находятся в Git; сами локальные пакеты, SDK и лицензии исключены. Подготовленный анализ и компиляция примеров работают без сети. Ansible загружает закреплённый публичный QNX-комплект и зависимости автоматически; установка оригинального лицензируемого QNX SDP и получение библиотек HU в playbook не входят.

## Сетевые инструменты и журналы

```sh
python3 tools/enet_identify.py --help
python3 tools/nbt_session.py init
python3 tools/nbt_session.py --dry-run collect
python3 tools/nbt_hsfz_trace.py --help
```

[enet_identify.py](../tools/enet_identify.py) реализует discovery и диагностическое чтение. [nbt_session.py](../tools/nbt_session.py) использует локальный конфиг из [шаблона](../config/nbt/session.example.json). dry-run показывает действия без подключения. Сбор файлов через SSH целиком на нашей HU ещё не проверен из-за отсутствия успешного входа.

[nbt_hsfz_trace.py](../tools/nbt_hsfz_trace.py) обрабатывает локальный PCAP и выдаёт метаданные без challenge, подписей, VIN и содержимого служебных параметров. [nbt_ssh_relay_probe.py](../tools/nbt_ssh_relay_probe.py) содержит ограниченный relay-клиент; unit-тесты не означают успешного открытия SSH на HU.

## Проверка перед публикацией

```sh
python3 -m unittest discover -s tools/tests -v
python3 tools/check_public.py
git diff --cached --check
```

check_public проверяет отслеживаемые файлы, в том числе блокирует принудительно добавленный research/. Содержательную проверку фактов автоматический сканер не заменяет.
