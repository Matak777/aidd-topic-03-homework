# investing (Investing Demo)

Учебный инвестиционный дашборд: каталог акций с HTML-интерфейсом и REST API,
фоновое обновление цен по расписанию (Celery beat), графики Plotly, загрузка
каталога из Tinkoff Invest API (gRPC).

Состав стека: Django 5.2 LTS, DRF 3.16, django-filter 25, Celery 5.6, Redis-клиент 6,
plotly 6, Tinkoff Invest SDK (вендорный, см. ниже). Проверено на Python 3.12.

> Современный SDK Tinkoff Invest (pip-пакеты `tinkoff-investments`/`tinkoff`)
> **удалён из PyPI**, официальный репозиторий заархивирован. Рабочая копия
> SDK сохранена в `vendor/tinkoff-investments` (источник, лицензия,
> происхождение и изменения описаны там же и в MODERNIZATION.md).

## Требования

- Python **3.12** (проверено на 3.12.15; поддержка 3.10–3.13 не проверялась —
  заявлять её не следует).
- [uv](https://docs.astral.sh/uv/) — для воспроизводимой установки. При желании
  можно установить зависимости из `requirements.txt` обычным pip.
- Redis — **только** для Celery beat/worker (фонового обновления цен).
  Без Redis сайт, API и графики работают; попытка запустить beat/worker
  завершится явной ошибкой соединения.
- Docker/PostgreSQL — необязательны: по умолчанию используется SQLite.

## Установка

```bash
# 1) Создать окружение из lock-файла (нужен uv)
uv sync                      # создаст .venv и установит зависимости
# либо без uv: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) Настройки окружения
cp .env.example .env         # значение по умолчанию работоспособно
#   SECRET_KEY                — замените на свой секрет в production
#   TINKOFF_TOKEN             — токен Tinkoff Invest API (пусто = без рыночных данных)
#   DATABASE=postgres + DB_*  — если хотите PostgreSQL вместо SQLite

# 3) Миграции
uv run python manage.py migrate        # или .venv/bin/python manage.py migrate

# 4) Запуск веб-приложения
uv run python manage.py runserver
#   http://127.0.0.1:8000/           — список акций
#   http://127.0.0.1:8000/api/v1/shares/ — REST API
```

## Что работает без внешних сервисов

- HTML-интерфейс: список/карточка акции, добавление, редактирование, удаление
  (аутентифицированным пользователем), пагинация, подсветка изменения цены.
- Регистрация и вход по email, выход.
- REST API `/api/v1/shares/`: CRUD, фильтры `?name=/?ticker=`, поиск `?search=`,
  сортировка `?ordering=name|ticker`, пагинация limit/offset.
- Регулярные тесты: `uv run python manage.py test`.

## Забираемый каталог и рыночные данные (Tinkoff API)

- `uv run python manage.py add_shares` — загрузка каталога акций.
- Задача `dashboard.tasks.update_last_price` — обновление `last_price` и признака
  `is_trend_high` (Celery beat запускает её каждые 10 секунд).
- Карточка акции строит график за 180 дней (дневные свечи).
- Без `TINKOFF_TOKEN` эти операции завершаются явной ошибкой
  (`TinkoffTokenMissing`), страница карточки остаётся доступной с сообщением
  «График недоступен: …». Живая интеграция требует действующего токена и
  доступного Tinkoff Invest API.

## Фоновое обновление цен (Celery) — нужен Redis

```bash
# 1) Redis (например, brew install redis && redis-server) или docker compose up redis

# 2) Worker и scheduler (в двух отдельных терминалах)
uv run celery -A config beat
uv run celery -A config worker -l info
```

Для Windows, как и ранее, поддерживается запуск worker через eventlet
(pip install eventlet cryptography; celery -A config worker -l info -P eventlet).

## PostgreSQL (опционально, как в исходном проекте)

```bash
# Вариант с Docker — docker-compose.yml сохранён:
docker compose up -d db redis
# в .env: DATABASE=postgres, DB_NAME=investing, DB_USER=postgres,
#         DB_PASSWORD=postgres, DB_HOST=localhost, DB_PORT=5432
uv run python manage.py migrate
```

Либо обычный PostgreSQL по тем же переменным `DB_*`.

## Заметки о конфигурации

- Все секреты задаются переменными окружения / файлом `.env` (см. `.env.example`).
  В конфигурации больше нет зашитых паролей базы и пустых секретных ключей
  без объяснения: значения по умолчанию пригодны только для локальной разработки
  и помечены в коде как dev-only.
- Ограничение времени внешних запросов Tinkoff API — `TINKOFF_TIMEOUT` (сек,
  умолчание 30). Допускается только конечное положительное значение:
  NaN/бесконечность/ноль/отрицательные дают ошибку конфигурации при старте.
  Дедлайн применяется к самому gRPC-вызову (RPC прерывается клиентом по
  превышению).
- Выход из аккаунта — POST-форма с CSRF (кнопка «Выход» на страницах);
  GET-ссылка выхода удалена намеренно: изменяющая сессию операция не должна
  выполняться небезопасным GET (GET `/userlogout/` отвечает 405).
- REST API: чтение публично, запись (POST/PUT/PATCH/DELETE) — только
  аутентифицированным (`IsAuthenticatedOrReadOnly`) — исправление дефекта
  Д12 из ANALYSIS.md, см. MODERNIZATION.md.

Подробный аудит изменений, проверенные требования и результаты запусков —
в `MODERNIZATION.md`; описание исходного состояния — в `ANALYSIS.md`.
