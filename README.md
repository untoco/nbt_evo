# NBT Evo

Инструменты для BMW NBT Evo / iDrive 6: диагностика ENET, сборка ARM32 QNX и локальная работа с Ghidra/REA.

В Git публикуются проверенные факты, работающий код, тесты, рецепты окружения и инструкции по их использованию.

- [Подтверждённые результаты и границы проверок](docs/facts.md).
- [Архитектура, подготовка и команды инструментов](docs/environment.md).
- [Обезличенные отчёты проверок](docs/evidence/).

```mermaid
flowchart TD
    M[macOS: Python, SSH, ENET] --> C[Автомобиль: ZGW и HU]
    M --> D[Docker / Colima]
    D --> Q[Компилятор ARM32 QNX]
    D --> R[Ghidra + REA]
    Q --> L[Локальные результаты .build]
    R --> L
```

Быстрая проверка из корня проекта:

```sh
python3 -m unittest discover -s tools/tests -v
python3 tools/check_public.py
```
