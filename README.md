# Investing Demo

Django-приложение для просмотра и управления списком акций, графиков котировок Tinkoff Invest и REST API.

## Требования

- Python 3.12 (локально проверена версия 3.12.15). Метаданные проекта допускают Python 3.12–3.14, но версии 3.13 и 3.14 в этой работе не проверялись.
- [uv](https://docs.astral.sh/uv/) для установки зафиксированных зависимостей.
- SQLite используется по умолчанию. PostgreSQL можно подключить настройками `.env`.
- Redis нужен только для запуска Celery worker и Beat. Django-сайт работает без Redis и Docker.
- Для запросов живых данных нужен действующий `TINKOFF_TOKEN`. Без него график показывает понятное сообщение, команда импорта и фоновое обновление явно сообщают об ошибке.

## Установка и локальный запуск без Docker

Установите Python 3.12+ и uv обычным для вашей системы способом. В корне репозитория создайте виртуальное окружение и установите версии из `uv.lock`:

```sh
uv venv --python 3.12
```

Активируйте окружение (или используйте путь к его `python` в команде `uv pip`):

```sh
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Установите приложение и зафиксированные зависимости:

```sh
uv sync --frozen
```

Tinkoff SDK входит в lock и устанавливается при каждой `uv sync`. В `vendor/tinkoff-investments` сохранён исходный код официального [репозитория SDK](https://github.com/Tinkoff/invest-python), upstream commit `1cc84950266d62cd0bc7a43ad764dddfd4252630`, лицензия Apache-2.0. Содержимое `tinkoff/` взято без изменений. Локальная упаковка удаляет upstream-зависимость `tinkoff>=0.1.1`, которую нельзя разрешить из доступного PyPI, и задаёт локальное описание/путь README; остальные runtime-зависимости сохранены с эквивалентными диапазонами версий. Источник, лицензия и изменения packaging metadata описаны в `vendor/tinkoff-investments/UPSTREAM.md`. Это исправление согласованности метаданных, а не утверждение о неработоспособности клиента.

Повторный `uv sync --frozen` сохраняет SDK, а `uv pip check` должен завершаться без несовместимостей.

Скопируйте пример окружения и при необходимости измените значения:

```sh
# macOS / Linux
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

В примере включена локальная SQLite; секрет Tinkoff не задан. Для локального запуска сайта не нужны ни Docker, ни PostgreSQL, ни Redis. Примените миграции и запустите сервер:

```sh
python manage.py migrate
python manage.py runserver
```

Откройте <http://127.0.0.1:8000/>. Создание администратора по желанию:

```sh
python manage.py createsuperuser
```

## Настройка

Настройки читаются из переменных окружения; `python-dotenv` автоматически загружает локальный `.env`, который исключён из Git.

| Переменная | Назначение |
| --- | --- |
| `DJANGO_DEBUG` | Режим отладки; по умолчанию `true` для локальной разработки. |
| `DJANGO_SECRET_KEY` | Секрет Django. Для `DJANGO_DEBUG=false` обязателен. |
| `DJANGO_ALLOWED_HOSTS` | Разделённый запятыми список хостов. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Разделённый запятыми список доверенных HTTPS-источников. |
| `DB_ENGINE` | `sqlite` (по умолчанию) или `postgresql`. |
| `SQLITE_NAME` | Путь к SQLite-файлу; по умолчанию `db.sqlite3` в корне проекта. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | Подключение к PostgreSQL, если выбрано `DB_ENGINE=postgresql`. |
| `TINKOFF_TOKEN` | Токен Tinkoff Invest; без значения живые запросы не выполняются. |
| `TINKOFF_RPC_TIMEOUT_SECONDS` | Положительный конечный deadline для каждого синхронного unary gRPC-вызова; по умолчанию `10` секунд. |
| `CELERY_BROKER_URL` | URL Redis broker; используется только Celery. |
| `CELERY_TASK_ALWAYS_EAGER` | Выполнение Celery task синхронно, в основном для локальных тестов. |

Для production задайте непредсказуемый `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, корректные allowed hosts и PostgreSQL credentials. Не коммитьте `.env`.

## Загрузка акций и рыночные данные

После установки SDK и настройки `TINKOFF_TOKEN` загрузите справочник из Tinkoff Invest:

```sh
python manage.py add_shares
```

Сервис графика, обновление цены и импорт справочника используют Tinkoff API. Без токена или при ошибке провайдера приложение показывает/логирует ошибку, а не выдает демонстрационные данные за настоящие. На все синхронные unary RPC установлен конечный deadline, ограниченный `TINKOFF_RPC_TIMEOUT_SECONDS`; превышение срока отображается как понятная ошибка для цены, графика или импорта. В тестах транспорт и внешние ответы подменяются mock-объектами.

## Celery и Redis (опционально)

Установите и запустите Redis отдельно от приложения. Затем в двух терминалах, с активированным `.venv`:

```sh
celery -A config worker -l info
```

```sh
celery -A config beat -l info
```

Celery Beat запускает обновление сохранённых цен каждые пять минут. Если Redis не запущен, worker/Beat не смогут подключиться к broker; веб-приложению для обычных HTTP-запросов Redis не нужен.

## REST API

Основной endpoint: <http://127.0.0.1:8000/api/v1/shares/>. Чтение доступно анонимно, изменения требуют аутентификации. Для фильтров доступны `name` и `ticker`, сортировки `name` и `ticker`, поиск по имени. Пагинация — limit/offset, размер страницы по умолчанию 100.

## Проверки

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate
python manage.py test
uv pip check
```

Тесты используют SQLite и моки для Tinkoff API. Они проверяют локальную логику, но не подтверждают доступность Redis, Tinkoff API или достоверность живых котировок.

## Управление зависимостями

- `pyproject.toml` задаёт поддерживаемые диапазоны прямых зависимостей и локальный SDK package.
- `uv.lock` фиксирует версии и источники приложения и SDK для воспроизводимой установки командой `uv sync --frozen`; повторная синхронизация сохраняет SDK.
- `requirements.txt` экспортирован из lock-файла с версиями и hash и ссылается на vendored SDK по относительному пути `./vendor/tinkoff-investments`.
