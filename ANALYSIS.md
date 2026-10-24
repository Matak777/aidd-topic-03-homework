# ANALYSIS.md — Investing Demo (задания №1–2)

Методика: статический анализ исходного кода; поведение Django 4.1.7 / DRF 3.14 перепроверено
эмпирически в изолированном окружении (проект при этом не изменялся и не запускался).
Подтверждения указаны в формате `полный/путь/от/корня/репозитория:строки`. Поведение,
взятое из внешних пакетов, приводится с явным указанием пакета и версии (например,
«пакет Django 4.1.7, модуль …») и локальным файлом проекта не является.

## 1. Назначение и технологический стек

Учебное приложение «инвестиционный дашборд»: каталог акций (`Share`) выгружается из Tinkoff
Invest API, последние цены периодически актуализируются фоновыми задачами Celery; данные
показываются через HTML-интерфейс и REST API (README.md:1-23).

Стек (requirements.txt:1-40):
- Django 4.1.7, DRF 3.14.0, django-filter 22.1, django-crispy-forms 2.0 + crispy-bootstrap3 2022.1;
- Celery 5.2.7, брокер Redis (redis-py 4.5.1), сохранение результатов задач не настроено;
- PostgreSQL (psycopg2-binary 2.9.5, БД «investing») и Redis — в docker-compose.yml:3-19;
- Tinkoff Invest SDK 0.2.0b50 + пакет tinkoff 0.1.1 (grpcio 1.51.3, protobuf 3.20.3,
  pydantic 1.10.6, tenacity 8.2.2);
- графики — plotly 5.13.1 (рендер графика на сервере, вставка HTML-блока в страницу);
- утилиты форматирования: black 23.1.0, isort 5.12.0;
- версия Python не зафиксирована (нет pyproject/setup.cfg; manage.py:1-22).

## 2. Структура проекта и архитектура

- `config/` — настройки (config/settings.py), корневые маршруты (config/urls.py:4-8),
  Celery-приложение (config/celery.py), wsgi/asgi.
- `dashboard/` — приложение-каталог: модель Share (dashboard/models.py:4-21), HTML-CRUD
  (dashboard/views.py:20-63), форма (dashboard/forms.py:6-11), админка (dashboard/admin.py:6-19),
  миграции 0001–0007 (dashboard/migrations/), сервисы market_data и graph (dashboard/services/),
  задача update_last_price (dashboard/tasks.py), команда add_shares
  (dashboard/management/commands/add_shares.py), тесты API (dashboard/tests.py:8-122),
  шаблоны (dashboard/templates/dashboard/) и статика.
- `dashboard/api/v1/` — версионированный REST API: router `shares`, ModelViewSet, сериализатор,
  фильтр (dashboard/api/v1/views.py:10-22, dashboard/api/v1/router.py:5-6,
  dashboard/api/v1/serializer.py:6-19).
- `user/` — регистрация/вход/выход по email (user/views.py:8-64); собственные модели не объявлены
  (user/models.py:1-3), шаблоны Bootstrap (user/templates/user/base.html).

Потоки данных:
1. Браузер → config/urls.py:4-8 → dashboard/user (CBV + шаблоны) → ORM → PostgreSQL.
2. REST-клиент → `/api/v1/shares` (dashboard/api/urls.py:3-5 → dashboard/api/v1/urls.py:5 →
   dashboard/api/v1/router.py:5-6) → DRF ViewSet → та же БД.
3. Celery beat каждые 10 с (config/celery.py:13-17) → брокер Redis db:0 (config/settings.py:141) →
   worker → задача update_last_price (dashboard/tasks.py:7-28) → gRPC Tinkoff
   (dashboard/services/market_data.py:6-11) → БД.
4. Страница акции синхронно запрашивает свечи за 180 дней и отображает HTML-график Plotly
   (dashboard/services/graph.py:11-36, dashboard/templates/dashboard/detail.html:22-25).
5. Логи пишутся в консоль и файл app.logs (config/settings.py:37-57).

## 3. Модель данных, обработка запросов и работа с БД

Сущность одна — `Share` (dashboard/models.py:4-21); пользователь — стандартный
`django.contrib.auth.models.User`; связей между сущностями нет: Share — общий каталог.

| Поле | Тип | Ограничения |
|---|---|---|
| id | BigAutoField | PK, создаётся автоматически |
| figi | CharField(255) | unique (dashboard/migrations/0002_update_share.py:12-16) |
| isin | CharField(255) | — |
| name | CharField(255) | verbose «Наименование компании» |
| ticker | CharField(10) | unique (dashboard/migrations/0002_update_share.py:17-21, dashboard/migrations/0006_add_verbose_name.py:12-16) |
| currency | CharField(20) | verbose «Валюта» |
| uid | CharField(255) | unique (dashboard/migrations/0003_add_uuid.py:12-17) |
| last_price | FloatField | null/blank, «Текущая цена» (dashboard/migrations/0004_add_last_price.py:19-23) |
| is_trend_high | BooleanField | null/blank, «Растет цена» (dashboard/migrations/0004_add_last_price.py:12-18) |
| updated_at | DateTimeField | auto_now (dashboard/migrations/0007_add_updated_at.py:12-16) |

Обработка HTML-запросов: список — ListView с пагинацией 100 и сортировкой по name
(dashboard/views.py:20-26); карточка — ручной View с `Share.objects.get(id=pk)`
(dashboard/views.py:29-38); добавление/изменение/удаление — Create/Update/DeleteView с формой
ShareModelForm (dashboard/forms.py:6-11, dashboard/views.py:41-63) и проверкой аутентификации
(dashboard/views.py:11-17). ModelForm с `fields = "__all__"` включает редактируемые поля модели;
автоматически создаваемый `id` и `updated_at` с `auto_now=True` (editable=False) в форму не входят
(поведение пакета Django 4.1.7, django/forms/models.py — fields_for_model исключает
авто-создаваемые и не редактируемые поля; перепроверено по пакету).
API: ModelViewSet + LimitOffsetPagination (PAGE_SIZE=100) + фильтры name/ticker (точное совпадение)
+ поиск по name + сортировка name/ticker (dashboard/api/v1/views.py:10-22,
config/settings.py:31-35).
Работа с БД: Django ORM; задача цен пишет через `QuerySet.update`, который не вызывает save() и
не обновляет auto_now-поля (dashboard/tasks.py:24-26); в прикладном коде нет явных транзакционных
блоков — вызовов `transaction.atomic` и т. п. в исходниках проекта нет (внутренние транзакции
Django при этом не отрицается: например, `get_or_create` в
dashboard/management/commands/add_shares.py:15-22 изменяет данные внутри собственной
транзакции пакета Django 4.1.7, django/db/models/query.py). `get_or_create` используется в прикладном коде только командой add_shares; записи также
создаются через HTML CreateView (dashboard/views.py:41-46), API ModelViewSet
(dashboard/api/v1/views.py:16-18) и `User.objects.create_user` при регистрации
(user/forms.py:51). Права на БД заданы
жёстко в config/settings.py:96-104.

## 4. Пользовательские сценарии и бизнес-правила

Сценарии:
1. Аноним открывает список акций, щёлкает по тикеру → карточка с графиком.
2. Вход по email/паролю; при неудаче — «Неверный логин или пароль! Повторите попытку.»
   (user/views.py:26-33, user/templates/user/auth.html:14).
3. Регистрация (email, пароль, повтор) с автоматическим входом и переходом к списку
   (user/views.py:44-49).
4. Выход — разлогин и возврат к списку (user/views.py:58-64, user/urls.py:10).
5. Аутентифицированный пользователь добавляет, редактирует и удаляет акции
   (dashboard/templates/dashboard/list.html:9-13,41-50).
6. Первичная загрузка каталога: `python manage.py add_shares` (README.md:9-10).
7. Фоновая актуализация цен по расписанию (README.md:12-16, config/celery.py:13-17).
8. REST-клиент смотрит, создаёт, изменяет и удаляет акции через `/api/v1/shares`.

Бизнес-правила и проверки:
- уникальность figi, ticker, uid на уровне БД; длина полей до 255/10/20 символов
  (dashboard/models.py:7-17);
- email — идентификатор входа, должен быть уникален (user/forms.py:39-46);
- пароль подтверждается повтором (user/forms.py:17-28,35-37);
- изменение каталога через HTML — только аутентифицированным (dashboard/views.py:11-17);
- доступ к рыночным данным требует TINKOFF_TOKEN (config/settings.py:134,
  dashboard/services/market_data.py:7, dashboard/services/graph.py:12);
- валидаторы пароля AUTH_PASSWORD_VALIDATORS заданы, но не применяются формой регистрации
  (config/settings.py:106-119, user/forms.py:48-53).

## 5. Восстановленные функциональные требования (фактически реализованные)

Наличие и состав тестов перенесены в раздел о проверках и покрытии (см. Д16) — тесты не
функциональное требование приложения, а артефакт проверки.

Т1. Главная страница — список акций: колонки «Тикер/Название/ISIN/Текущая цена», тикер — ссылка на карточку; пагинация по 100, сортировка по названию; при пустой базе — «Акций еще нет» (dashboard/views.py:20-26, dashboard/urls.py:14, dashboard/templates/dashboard/list.html:16-58,60-80,83-85).
Т2. Цена подсвечивается фоном ячейки: зелёным при is_trend_high, красным в остальных случаях; выводится вместе с валютой (dashboard/templates/dashboard/list.html:35-40).
Т3. Карточка акции: название, текущая цена с валютой, время последнего обновления и график цены за 180 дней (дневные свечи) без аутентификации (dashboard/views.py:29-38, dashboard/services/graph.py:14-20, dashboard/templates/dashboard/detail.html:11-14,22-25).
Т4. Добавление акции — только аутентифицированным, форма ShareModelForm включает редактируемые поля модели (автоматически создаваемый id и updated_at с auto_now=True в неё не входят); после сохранения — возврат к списку (dashboard/urls.py:16, dashboard/views.py:41-46, dashboard/forms.py:6-11, dashboard/templates/dashboard/create.html:13-19).
Т5. Редактирование акции — как Т4, по id (dashboard/urls.py:17, dashboard/views.py:49-55).
Т6. Удаление — только аутентифицированным, со страницей подтверждения «Вы уверены, что хотите удалить инструмент…» (dashboard/urls.py:18, dashboard/views.py:58-63, dashboard/templates/dashboard/delete.html:9-13).
Т7. Неаутентифицированный пользователь при обращении к страницам изменения данных перенаправляется на главную без сообщения (dashboard/views.py:11-17).
Т8. Шапка отражает состояние входа: «Привет, email» и кнопка «Выход» для вошедших, кнопка «Войти» для остальных (dashboard/templates/dashboard/base.html:37-43).
Т9. Регистрация: поля email/пароль/повтор; отказ с сообщением при несовпадении паролей («Введенные пароли не совпадают») и при уже занятом email (user/forms.py:30-46, user/templates/user/registration.html:18).
Т10. Вход выполняется по email (приводится к нижнему регистру) и паролю; при неудаче показывается сообщение об ошибке (user/views.py:16-33, user/templates/user/auth.html:14).
Т11. Выход — разлогин и редирект на список (user/urls.py:10, user/views.py:58-64).
Т12. Команда `add_shares` загружает все акции из API Tinkoff и сохраняет их через get_or_create по figi/name/isin/ticker/currency/uid (README.md:9-10, dashboard/management/commands/add_shares.py:8-22).
Т13. Celery beat каждые 10 с запускает задачу, которая для всех акций получает последнюю цену и сохраняет last_price с признаком направления относительно предыдущего значения; ошибки акции журналируются и не прерывают обход (README.md:12-16, config/celery.py:13-17, dashboard/tasks.py:7-28).
Т14. REST API `/api/v1/shares`: полный CRUD; сериализатор без uid; пагинация limit/offset по 100; фильтры `?name=`/`?ticker=` (точное совпадение); `?search=` по name — в DRF (пакет djangorestframework 3.14.0, rest_framework/filters.py, SearchFilter.construct_search: префикс `$` даёт iregex; проверено по пакету, префикс задан в dashboard/api/v1/views.py:22); сортировка по name/ticker (dashboard/api/v1/views.py:16-22, dashboard/api/v1/serializer.py:6-19, dashboard/api/v1/router.py:5-6, dashboard/api/urls.py:4, config/settings.py:31-35).
Т15. Админка: список figi/isin/name/ticker/currency, поиск по figi/isin/name/ticker, форма включает цену и признак тренда (dashboard/admin.py:6-19, config/urls.py:5).
Т16. Логирование INFO/ERROR в консоль и файл app.logs: получение цен и ошибки задач (config/settings.py:37-57, dashboard/services/market_data.py:10, dashboard/tasks.py:21,27).

## 6. Дефекты, противоречия и ограничения (не требования)

Безопасность и конфигурация:
- Д1. SECRET_KEY пуст (config/settings.py:6), DEBUG=True (8), ALLOWED_HOSTS=[] (10), пароль БД
  в коде (100-102); deploy-проверки Django дают security.W009/W018/W020 (проверено эмпирически).
- Д2. TINKOFF_TOKEN пуст (config/settings.py:134) — карточка (dashboard/services/graph.py:12),
  задача цен (dashboard/services/market_data.py:7) и add_shares
  (dashboard/management/commands/add_shares.py:12) неработоспособны без его заполнения.
- Д3. Цены без дробной части: берётся только `units` из Quotation
  (dashboard/services/market_data.py:11, dashboard/services/graph.py:20), поле `nano` игнорируется;
  деньги хранятся в FloatField (dashboard/models.py:13).

Корректность данных и обработка ошибок:
- Д4. Спорное фактическое поведение, бизнес-правилом не является: признак «растёт» вычисляется
  сравнением с предыдущей ценой, а при равной цене или любой ошибке сравнения безусловно
  ставится «растёт» (dashboard/tasks.py:15-22) — зелёная подсветка обманчива
  (dashboard/templates/dashboard/list.html:35-40); пустой ответ API даёт IndexError и запись
  в журнал (dashboard/services/market_data.py:9, dashboard/tasks.py:27-28).
- Д5. `QuerySet.update` не обновляет auto_now updated_at (dashboard/tasks.py:24-26,
  dashboard/models.py:17; проверено) — карточка показывает устаревшее время обновления.
- Д6. HTML-карточка несуществующего id приводит к 500 (dashboard/views.py:32), тогда как API
  отдаёт 404 (dashboard/tests.py:54-56,120-122).
- Д7. Регистрация падает (KeyError → 500) при невалидном или пустом email: form.clean() напрямую
  берёт cleaned_data["email"] (user/forms.py:32-34; проверено на реальной форме); ветка
  отображения ошибок user/views.py:54 работает только для не-полевых ошибок (занятый email и
  несовпадение паролей обрабатываются — проверено).
- Д8. Вход не согласован с регистрацией: authenticate() приводит email к нижнему регистру
  (user/views.py:19), а username сохраняется как введено (user/forms.py:51) — при смешанном
  регистре вход невозможен (проверено).
- Д9. Валидаторы пароля не применяются в регистрации: пароль «1» принимается (проверено;
  user/forms.py:48-53 не вызывает password_validation).
- Д10. Email не уникален на уровне БД, проверка в clean() регистрозависима и не атомарна
  (user/forms.py:39-46) — возможны дубли.
- Д11. add_shares: get_or_create по всем полям
  (dashboard/management/commands/add_shares.py:15-22) — при изменении любого поля существующей
  акции возникнет IntegrityError по unique figi.
- Д12. API открыт всем: полный CRUD без аутентификации (DRF по умолчанию AllowAny — пакет
  djangorestframework 3.14.0, rest_framework/settings.py: DEFAULT_PERMISSION_CLASSES включают
  AllowAny, в проекте не переопределён; проверено; dashboard/api/v1/views.py:16-22,
  config/settings.py:31-35) — противоречит ограничению UI (dashboard/views.py:11-17).

Эксплуатация и производительность:
- Д13. Запись beat «add-every-5-min» с фактическим интервалом 10 с (config/celery.py:14-16);
  CELERY_TIMEZONE Europe/Moscow против TIME_ZONE UTC (config/settings.py:124,137); защита от
  параллельных запусков задачи отсутствует.
- Д14. Обход каталога в задаче строго последовательный (dashboard/tasks.py:11-28), при этом
  gRPC-клиент создаётся заново на каждую акцию (dashboard/services/market_data.py:7).
  Фактическая длительность задачи не измерялась; чем больше каталог, тем выше риск, что задача
  не уложится в заданный интервал 10 с (см. Д13 — защита от параллельных запусков отсутствует,
  поэтому запуски могут накладываться). Кроме того, карточка акции синхронно запрашивает
  180-дневные свечи при каждом открытии (dashboard/services/graph.py:11-21).

- Д15. Поиск `?search=` — iregex (проверено; пакет djangorestframework 3.14.0,
  rest_framework/filters.py, SearchFilter.construct_search, префикс `$` задан в
  dashboard/api/v1/views.py:22): спецсимволы могут приводить к ошибке выполнения запроса.
  Поведение на конкретной СУБД не проверялось (см. Н5).

Проверки и покрытие тестами:
- Д16. Тесты существуют только для REST API и главной HTML-страницы: dashboard/tests.py:8-122 —
  DashboardTests (APITestCase) проверяет доступность HTML-главной страницы (32-34), список API
  (36-46), карточку (48-52), 404 (54-56), обновление успех/ошибка (58-89), создание
  успех/ошибка (91-109), удаление успех/404 (111-122). HTML-CRUD, регистрация, вход, задача
  update_last_price и команда add_shares тестами не покрыты. История миграций содержит правку
  собственных ошибок verbose_name (dashboard/migrations/0004_add_last_price.py:29-33 — name
  получил verbose «Валюта», исправлено в dashboard/migrations/0005_add_verbose_name.py:12-18).

## 7. Неопределённости

- Н1. Версия Python не зафиксирована; работоспособность стека на текущем окружении не проверялась —
  запуск и модернизация отнесены к заданию №3 (HOMEWORK.md:14-16,86-104).
- Н2. Приложение в этом анализе не запускалось: runtime-поведение с пустыми SECRET_KEY/TINKOFF_TOKEN,
  реальные ответы Tinkoff API и наполнение БД не наблюдались.
- Н3. Неясно, является ли общий каталог, редактируемый всеми аутентифицированными пользователями,
  сознательным решением — привязки Share к пользователю нет (dashboard/models.py:4-21,
  dashboard/views.py:41-63).
- Н4. Целевой интервал обновления цен неоднозначен: «add-every-5-min» против 10 с
  (config/celery.py:14-16).
- Н5. Поведение поиска API при невалидном регулярном выражении на конкретной СУБД не проверялось.
- Н6. Способ поставки токена не определён: README заявляет «TINKOFF_TOKEN указывается в
  config/settings.py» (README.md:6-7), переменные окружения в config/settings.py не используются.
