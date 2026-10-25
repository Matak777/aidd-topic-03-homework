from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Share(models.Model):
    """Акция"""

    class PriceSource(models.TextChoices):
        TINKOFF = "tinkoff", "Tinkoff Invest"
        MANUAL = "manual", "Введено вручную"

    IDENTITY_FIELDS = ("figi", "isin", "ticker", "uid")

    figi = models.CharField(max_length=255, unique=True)
    isin = models.CharField(max_length=255)
    name = models.CharField(max_length=255, verbose_name="Наименование компании")
    ticker = models.CharField(max_length=10, unique=True, verbose_name="Тикер")
    currency = models.CharField(max_length=20, verbose_name="Валюта")
    uid = models.CharField(max_length=255, unique=True)
    last_price = models.FloatField(blank=True, null=True, verbose_name="Текущая цена")
    last_price_source = models.CharField(
        max_length=20,
        choices=PriceSource.choices,
        blank=True,
        null=True,
        editable=False,
    )
    last_price_observed_at = models.DateTimeField(blank=True, null=True, editable=False)
    is_trend_high = models.BooleanField(
        blank=True, null=True, verbose_name="Растет цена"
    )
    updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name = "Акция"
        verbose_name_plural = "Акции"

    def apply_update_policy(self, previous, *, explicit_price=False):
        identity_changed = any(
            getattr(self, field) != getattr(previous, field)
            for field in self.IDENTITY_FIELDS
        )
        currency_changed = self.currency != previous.currency

        if identity_changed and self.pk and self.price_observations.exists():
            changed = [
                field
                for field in self.IDENTITY_FIELDS
                if getattr(self, field) != getattr(previous, field)
            ]
            raise ValidationError(
                {
                    field: (
                        "Нельзя менять идентификатор инструмента, пока у него "
                        "сохранена история цен. Создайте новый инструмент."
                    )
                    for field in changed
                }
            )

        if explicit_price:
            self.last_price_source = (
                self.PriceSource.MANUAL
                if self.last_price is not None
                else None
            )
            self.last_price_observed_at = None
            self.is_trend_high = None
        elif identity_changed or currency_changed:
            self.last_price = None
            self.last_price_source = None
            self.last_price_observed_at = None
            self.is_trend_high = None


class PriceObservation(models.Model):
    class Source(models.TextChoices):
        TINKOFF = "tinkoff", "Tinkoff Invest"
        DEMO = "demo", "Демонстрационные данные"

    share = models.ForeignKey(
        Share,
        on_delete=models.CASCADE,
        related_name="price_observations",
    )
    price = models.DecimalField(max_digits=30, decimal_places=9)
    currency = models.CharField(max_length=20)
    source = models.CharField(max_length=20, choices=Source.choices)
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("observed_at", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("share", "source", "observed_at"),
                name="unique_share_price_observation",
            )
        ]
        indexes = [
            models.Index(
                fields=("share", "observed_at"),
                name="price_share_observed_idx",
            )
        ]

    def __str__(self):
        return f"{self.share.ticker}: {self.price} {self.currency} @ {self.observed_at}"
