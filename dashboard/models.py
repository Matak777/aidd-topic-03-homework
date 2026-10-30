from decimal import Decimal

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
        """Есть ли у акции накопленные наблюдения цены."""
        return bool(self.pk) and self.price_observations.exists()

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
        max_length=20,
        choices=[
            (SOURCE_TINKOFF, "Tinkoff Invest API"),
            (SOURCE_DEMO, "Демонстрационные данные"),
        ],
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
