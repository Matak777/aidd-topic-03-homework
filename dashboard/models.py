from decimal import Decimal

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class Share(models.Model):
    """Акция"""

    figi = models.CharField(max_length=255, unique=True)
    isin = models.CharField(max_length=255)
    name = models.CharField(max_length=255, verbose_name="Наименование компании")
    ticker = models.CharField(max_length=10, unique=True, verbose_name="Тикер")
    currency = models.CharField(max_length=20, verbose_name="Валюта")
    uid = models.CharField(max_length=255, unique=True)
    last_price = models.FloatField(blank=True, null=True, verbose_name="Текущая цена")
    # Происхождение текущей цены (fix 3): откуда попало значение last_price.
    # NULL — происхождение неизвестно (записи до миграции 0009 и вручную
    # созданные без цены); показывается пользователю честно как неизвестное.
    LAST_PRICE_SOURCE_TINKOFF = "tinkoff"
    LAST_PRICE_SOURCE_DEMO = "demo"
    LAST_PRICE_SOURCE_MANUAL = "manual"
    LAST_PRICE_SOURCE_CHOICES = [
        (LAST_PRICE_SOURCE_TINKOFF, "Tinkoff Invest API"),
        (LAST_PRICE_SOURCE_DEMO, "Демонстрационный источник"),
        (LAST_PRICE_SOURCE_MANUAL, "Введено вручную"),
    ]
    last_price_source = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=LAST_PRICE_SOURCE_CHOICES,
        verbose_name="Источник текущей цены",
    )
    is_trend_high = models.BooleanField(
        blank=True, null=True, verbose_name="Растет цена"
    )
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)

    # Атрибуты идентичности инструмента (fix 4): их смена у акции,
    # у которой уже накоплена история наблюдений, подменяет инструмент,
    # к которому относится история, — запрещено во всех путях
    # (API, HTML-формы, admin, импорт).
    IDENTITY_FIELDS = ("figi", "uid", "ticker")

    def has_price_history(self) -> bool:
        """Есть ли у акции накопленные наблюдения или портфельные операции.

        Операции портфелей (Trade) тоже привязывают идентичность: у акции,
        по которой есть виртуальные сделки, смена figi/uid/ticker/валюты
        переписала бы финансовую историю (снимок обязан совпадать с
        инструментом) — считаем её «историей» наравне с наблюдениями.
        """
        if not self.pk:
            return False
        return self.price_observations.exists() or self.trades.exists()

    def protected_identity_changes(self, new_values: dict) -> list[str]:
        """Поля идентичности/валюты, которые попытается изменить запись.

        new_values — словарь желаемых значений (figi/uid/ticker/currency);
        возвращает имена запрещённых полей для акции с историей наблюдений.
        Валюта включена: смена валюты не должна смешивать накопленные серии
        (наблюдения, у которых валюта отличается от инструмента, искажают
        смысл «текущая цена в валюте инструмента»).
        """
        if not self.has_price_history():
            return []
        changed = []
        for field in (*self.IDENTITY_FIELDS, "currency"):
            if field in new_values and new_values[field] != getattr(
                self, field
            ):
                changed.append(field)
        return changed

    class Meta:
        verbose_name = "Акция"
        verbose_name_plural = "Акции"


class PriceObservation(models.Model):
    """Наблюдение рыночной цены инструмента (группа 3, FEATURES.md).

    Точная цена хранится Decimal'ом (exact units/nano из gRPC Quotation);
    время котировки (от источника) отделено от времени получения
    (auto_now_add). Источник данных — обязательный атрибут:
    "tinkoff" (реальный API) или "demo" (явно включаемый синтетический
    режим для демонстрации без токена и не выдаётся за живые котировки).
    """

    SOURCE_TINKOFF = "tinkoff"
    SOURCE_DEMO = "demo"
    SOURCE_CHOICES = [
        (SOURCE_TINKOFF, "Tinkoff Invest API"),
        (SOURCE_DEMO, "Демонстрационные данные"),
    ]

    share = models.ForeignKey(
        Share,
        on_delete=models.CASCADE,
        related_name="price_observations",
    )
    # Точная цена хранится как текст десятичного числа, а не NUMERIC:
    # на SQLite Django пишет Decimal точно (адаптер Decimal→str), но читает
    # NUMERIC-поля через create_decimal_from_float(prec=15) — молчаливая
    # потеря знаков (1234567.123456789 → ...6790); кроме того NUMERIC-аффинность
    # SQLite конвертирует текст в REAL ещё на записи. TEXT-колонка даёт
    # точный roundtrip (см. SQLiteDecimalRoundtripTests); Decimal-значение
    # доступно через price_decimal. Уже сохранённые значения в ранее
    # округлённых записях восстановлению не подлежат — они переносятся как есть.
    price = models.CharField(
        max_length=40,
        validators=[
            RegexValidator(
                regex=r"^-?\d+(\.\d+)?$",
                message=(
                    "Цена должна быть десятичным числом в текстовом "
                    "представлении (например, '123.45')."
                ),
            )
        ],
        verbose_name="Точная цена (текст Decimal)",
    )
    currency = models.CharField(max_length=20, verbose_name="Валюта")
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES
    )
    quote_time = models.DateTimeField(verbose_name="Время котировки")
    received_at = models.DateTimeField(
        auto_now_add=True, verbose_name="Время получения"
    )


    class Meta:
        verbose_name = "Наблюдение цены"
        verbose_name_plural = "Наблюдения цен"
        # Ключ дедупликации повторной доставки одного наблюдения:
        # тот же инструмент, источник и время котировки — то же самое
        # наблюдение (повторная запись не создаёт дубликат); та же цена
        # в другое время — другое наблюдение (quote_time различен).
        constraints = [
            models.UniqueConstraint(
                fields=["share", "source", "quote_time"],
                name="unique_observation_share_source_quote_time",
            )
        ]
        indexes = [models.Index(fields=["share", "quote_time"])]
        ordering = ["quote_time"]

    @property
    def price_decimal(self) -> Decimal:
        """Точное Decimal из сохранённого текста (без float-конверсии)."""
        return Decimal(self.price)

    def __str__(self):
        return f"{self.share.ticker} {self.price} @ {self.quote_time:%Y-%m-%dT%H:%M:%S}"


class ExactAmountField(models.CharField):
    """Точная десятичная величина (деньги/количество), текст Decimal.

    Причина — см. коммент у PriceObservation.price: NUMERIC-представление
    на SQLite (аффинность + чтение через float) молча теряет знаки; текст
    даёт точный roundtrip. Допустимая строка — десятичное число
    (например, '123.45'); неконечные (NaN/inf) и экспоненциальные записи
    валидатором запрещены. max_length=49 покрывает максимум политики
    точности сервиса: 30 цифр целой части + '.' + 18 цифр дробной;
    фактический допускаемый диапазон (значащие цифры, хвостовые нули
    не считаются) проверяется в сервисе (см. portfolio_accounting).
    """

    def __init__(self, verbose_name=None, **kwargs):
        kwargs.setdefault("max_length", 49)
        kwargs.setdefault(
            "validators",
            [
                RegexValidator(
                    regex=r"^-?\d+(\.\d+)?$",
                    message=(
                        "Значение должно быть десятичным числом в текстовом "
                        "представлении (например, '123.45')."
                    ),
                )
            ],
        )
        super().__init__(verbose_name=verbose_name, **kwargs)


def format_exact(value: Decimal) -> str:
    """Точная строка Decimal (без экспоненциальной записи)."""
    return format(value, "f")


class Portfolio(models.Model):
    """Личный виртуальный портфель (этап 2, F2.1).

    Пополнения/вывод/кредит/FX/шорт — вне этапа. Валюта одна, источник
    оценки выбирается явно при создании; сама модель не меняет его.
    """

    VALUATION_SOURCE_DEMO = "demo"
    VALUATION_SOURCE_TINKOFF = "tinkoff"
    VALUATION_SOURCE_CHOICES = [
        (
            VALUATION_SOURCE_DEMO,
            "Демонстрационный источник (синтетические цены)",
        ),
        (VALUATION_SOURCE_TINKOFF, "Tinkoff Invest API"),
    ]

    user = models.ForeignKey(
        getattr(settings, "AUTH_USER_MODEL", "auth.User"),
        on_delete=models.CASCADE,
        related_name="portfolios",
        verbose_name="Владелец",
    )
    name = models.CharField(max_length=255, verbose_name="Название")
    base_currency = models.CharField(max_length=20, verbose_name="Валюта портфеля")
    valuation_source = models.CharField(
        max_length=20,
        choices=VALUATION_SOURCE_CHOICES,
        verbose_name="Источник оценки",
    )
    # Точные точные Decimal-тексты (ExactAmountField): и стартовый баланс,
    # и текущий cash — источник истины для расчётов.
    initial_cash = ExactAmountField(verbose_name="Стартовый виртуальный баланс")
    cash = ExactAmountField(verbose_name="Виртуальный денежный остаток")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Виртуальный портфель"
        verbose_name_plural = "Виртуальные портфели"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                name="unique_portfolio_user_name",
            )
        ]
        ordering = ["created_at"]

    @property
    def initial_cash_decimal(self) -> Decimal:
        """Точный стартовый баланс (Decimal из текста, без float)."""
        return Decimal(self.initial_cash)

    @property
    def cash_decimal(self) -> Decimal:
        """Точный текущий остаток (Decimal из текста, без float)."""
        return Decimal(self.cash)

    def __str__(self):
        return f"{self.name} ({self.user})"


class Trade(models.Model):
    """Виртуальная операция портфеля — постоянный снимок исполнения.

    Журнал — источник истины: операции не редактируются и не удаляются
    через доступные интерфейсы. Идентичность инструмента копируется
    в снимок (figi/ticker/название/валюта), поэтому удаление акции
    не уничтожает и не переписывает финансовую историю (share —
    SET_NULL); удаление портфеля с историей запрещено (PROTECT →
    понятный отказ в HTML, а не 500 и не потеря записей).
    """

    SIDE_BUY = "BUY"
    SIDE_SELL = "SELL"
    SIDE_CHOICES = [(SIDE_BUY, "Покупка (BUY)"), (SIDE_SELL, "Продажа (SELL)")]

    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.PROTECT,
        related_name="trades",
        verbose_name="Портфель",
    )
    share = models.ForeignKey(
        "dashboard.Share",
        on_delete=models.SET_NULL,
        related_name="trades",
        null=True,
        blank=True,
        verbose_name="Инструмент (текущий)",
    )
    # --- постоянный снимок идентичности на момент исполнения ---
    figi = models.CharField(max_length=63, verbose_name="FIGI (снимок)")
    ticker = models.CharField(max_length=31, verbose_name="Тикер (снимок)")
    share_name = models.CharField(max_length=255, verbose_name="Название (снимок)")
    share_currency = models.CharField(max_length=20, verbose_name="Валюта инструмента (снимок)")
    # --- снимок исполнения ---
    side = models.CharField(
        max_length=10,
        choices=SIDE_CHOICES,
        verbose_name="Сторона",
    )
    quantity = ExactAmountField(verbose_name="Количество (> 0)")
    price = ExactAmountField(verbose_name="Цена исполнения — ручная (> 0)")
    commission = ExactAmountField(verbose_name="Комиссия (>= 0)")
    currency = models.CharField(max_length=20, verbose_name="Валюта сделки")
    note = models.CharField(max_length=255, blank=True, verbose_name="Заметка")
    executed_at = models.DateTimeField(verbose_name="Время операции")
    recorded_at = models.DateTimeField(auto_now_add=True)
    # --- идемпотентность повторной отправки ---
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="Ключ идемпотентности",
    )
    payload_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "Виртуальная операция"
        verbose_name_plural = "Виртуальные операции"
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "idempotency_key"],
                name="unique_trade_portfolio_idempotency_key",
            )
        ]
        # устойчивый порядок: одинаковые времена — по id
        ordering = ["executed_at", "id"]
        indexes = [models.Index(fields=["portfolio", "executed_at"])]

    # --- точные Decimal-доступники (текст хранение) ---
    @property
    def quantity_decimal(self) -> Decimal:
        return Decimal(self.quantity)

    @property
    def price_decimal(self) -> Decimal:
        return Decimal(self.price)

    @property
    def commission_decimal(self) -> Decimal:
        return Decimal(self.commission)

    def __str__(self):
        return f"{self.side} {self.quantity} {self.ticker} @ {self.price}"


# --- Персональный мониторинг (этап 3, группа 1) ----------------------------


class Watchlist(models.Model):
    """Личный именованный список наблюдения (F1.1).

    Виден и изменяем только владельцем; то же имя у другого владельца
    допустимо (уникальность (user, name)).
    """

    user = models.ForeignKey(
        getattr(settings, "AUTH_USER_MODEL", "auth.User"),
        on_delete=models.CASCADE,
        related_name="watchlists",
        verbose_name="Владелец",
    )
    name = models.CharField(max_length=255, verbose_name="Название")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Список наблюдения"
        verbose_name_plural = "Списки наблюдения"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "name"],
                name="unique_watchlist_user_name",
            )
        ]
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.user_id})"


class WatchlistItem(models.Model):
    """Инструмент в списке наблюдения; добавление без дублей (F1.1)."""

    watchlist = models.ForeignKey(
        Watchlist,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Список",
    )
    share = models.ForeignKey(
        Share,
        on_delete=models.CASCADE,
        related_name="watchlist_items",
        verbose_name="Инструмент",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Инструмент в списке"
        verbose_name_plural = "Инструменты в списках"
        constraints = [
            models.UniqueConstraint(
                fields=["watchlist", "share"],
                name="unique_watchlist_item",
            )
        ]
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.watchlist.name}: {self.share.ticker}"


class WatchRule(models.Model):
    """Правило мониторинга (F1.2/F1.3): ≥/≤ порога, источник, валюта.

    Оба направления связки — отдельными правилами. Состояние события
    живёт здесь (armed/triggered) и опирается на курсор обработанных
    наблюдений (см. dashboard.services.watch_monitor): в БД хранится
    и оно, и защита от повторов.
    """

    DIRECTION_GTE = ">="
    DIRECTION_LTE = "<="
    DIRECTION_CHOICES = [
        (DIRECTION_GTE, "Цена ≥ порога"),
        (DIRECTION_LTE, "Цена ≤ порога"),
    ]
    STATE_ARMED = "armed"
    STATE_TRIGGERED = "triggered"
    STATE_CHOICES = [
        (STATE_ARMED, "Ожидает события"),
        (STATE_TRIGGERED, "Условие выполнено"),
    ]

    user = models.ForeignKey(
        getattr(settings, "AUTH_USER_MODEL", "auth.User"),
        on_delete=models.CASCADE,
        related_name="watch_rules",
        verbose_name="Владелец",
    )
    share = models.ForeignKey(
        Share,
        on_delete=models.CASCADE,
        related_name="watch_rules",
        verbose_name="Инструмент",
    )
    direction = models.CharField(
        max_length=2,
        choices=DIRECTION_CHOICES,
        verbose_name="Направление",
    )
    threshold = ExactAmountField(
        verbose_name="Порог (> 0)",
    )
    source = models.CharField(
        max_length=20,
        choices=PriceObservation.SOURCE_CHOICES,
        verbose_name="Источник оценки",
    )
    currency = models.CharField(max_length=20, verbose_name="Валюта")
    enabled = models.BooleanField(default=True, verbose_name="Включено")
    state = models.CharField(
        max_length=16,
        choices=STATE_CHOICES,
        default=STATE_ARMED,
        verbose_name="Состояние",
    )
    cursor_id = models.BigIntegerField(
        default=0,
        verbose_name="Курсор обработанных наблюдений",
    )
    head_quote_time = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Время последнего применённого состояния",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Правило мониторинга"
        verbose_name_plural = "Правила мониторинга"
        # намеренно БЕЗ уникальности по (direction, threshold) и пр.:
        # повторные события после выхода/входа всегда другое наблюдение —
        # их запрещено ограничением (см. WatchEvent)
        indexes = [
            models.Index(fields=["share", "enabled"]),
            models.Index(fields=["user", "enabled"]),
        ]
        ordering = ["id"]

    def save(self, *args, **kwargs):
        # валюта сравнивается без регистра (USD/usd — одна валюта);
        # хранится нормализованной
        self.currency = (self.currency or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def threshold_decimal(self) -> "Decimal":
        """Точный Decimal порога из текста (как в наблюдениях)."""
        from decimal import Decimal as _Decimal

        return _Decimal(self.threshold)

    def __str__(self):
        return (
            f"{self.share.ticker} {self.direction} {self.threshold} "
            f"({self.source}/{self.currency}, {'вкл' if self.enabled else 'откл'})"
        )


class WatchEvent(models.Model):
    """Уведомление о событии правила (F1.3, этап 3).

    Все сведения хранятся снимком на момент срабатывания: изменение или
    удаление правила/инструмента/наблюдения не переписывает доставленное
    уведомление. Дубль исключён уникальностью (правило, наблюдение);
    повторное событие после выхода/входа — другое наблюдение, оно не
    запрещено этой уникальностью.
    """

    user = models.ForeignKey(
        getattr(settings, "AUTH_USER_MODEL", "auth.User"),
        on_delete=models.CASCADE,
        related_name="watch_events",
        verbose_name="Владелец",
    )
    watch_rule = models.ForeignKey(
        WatchRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name="Правило",
    )
    observation = models.ForeignKey(
        PriceObservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="watch_events",
        verbose_name="Наблюдение",
    )
    # снимок для переживания удаления правила/инструмента
    figi = models.CharField(max_length=32, default="")
    ticker = models.CharField(max_length=32, default="")
    share_name = models.CharField(max_length=128, default="")
    share_currency = models.CharField(max_length=20, default="")
    direction = models.CharField(max_length=2, choices=WatchRule.DIRECTION_CHOICES)
    threshold = ExactAmountField()
    source = models.CharField(max_length=20)
    currency = models.CharField(max_length=20)
    observed_price = ExactAmountField(verbose_name="Цена (снимок)")
    quote_time = models.DateTimeField(verbose_name="Время котировки (снимок)")
    message = models.CharField(max_length=255, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False, verbose_name="Прочитано")
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление мониторинга"
        verbose_name_plural = "Уведомления мониторинга"
        constraints = [
            models.UniqueConstraint(
                fields=["watch_rule", "observation"],
                name="unique_watch_event_rule_observation",
            )
        ]
        indexes = [models.Index(fields=["user", "-id"])]
        ordering = ["-id"]

    def is_demo(self) -> bool:
        return self.source == PriceObservation.SOURCE_DEMO

    def __str__(self):
        return f"{self.ticker} {self.direction} {self.threshold} @ {self.observed_price}"
