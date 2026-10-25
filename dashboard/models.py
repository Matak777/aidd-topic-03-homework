from django.core.exceptions import ValidationError
from django.conf import settings
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
        currency_changed = (
            self.currency.casefold() != previous.currency.casefold()
        )

        has_portfolio_history = self.pk and (
            self.portfolio_positions.exists()
            or self.portfolio_transactions.exists()
        )
        has_price_history = self.pk and self.price_observations.exists()

        if identity_changed and (has_price_history or has_portfolio_history):
            changed = [
                field
                for field in self.IDENTITY_FIELDS
                if getattr(self, field) != getattr(previous, field)
            ]
            raise ValidationError(
                {
                    field: (
                        "Нельзя менять идентификатор инструмента, пока у него "
                        "сохранена история цен или виртуальных операций. "
                        "Создайте новый инструмент."
                    )
                    for field in changed
                }
            )

        if currency_changed and has_portfolio_history:
            raise ValidationError(
                {
                    "currency": (
                        "Нельзя менять валюту инструмента, связанного с "
                        "виртуальной позицией или журналом операций."
                    )
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


class Watchlist(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="watchlists",
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_watchlist_name_per_owner",
            )
        ]

    def __str__(self):
        return self.name


class WatchlistItem(models.Model):
    watchlist = models.ForeignKey(
        Watchlist,
        on_delete=models.CASCADE,
        related_name="items",
    )
    share = models.ForeignKey(
        Share,
        on_delete=models.CASCADE,
        related_name="watchlist_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("share__ticker", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("watchlist", "share"),
                name="unique_share_per_watchlist",
            )
        ]

    def __str__(self):
        return f"{self.watchlist.name}: {self.share.ticker}"


class WatchRule(models.Model):
    class Direction(models.TextChoices):
        AT_LEAST = "gte", "Цена не ниже порога (>=)"
        AT_MOST = "lte", "Цена не выше порога (<=)"

    item = models.ForeignKey(
        WatchlistItem,
        on_delete=models.CASCADE,
        related_name="rules",
    )
    source = models.CharField(max_length=20, choices=PriceObservation.Source.choices)
    currency = models.CharField(max_length=20)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    threshold = models.DecimalField(max_digits=30, decimal_places=9)
    enabled = models.BooleanField(default=True)
    armed = models.BooleanField(default=True)
    state_initialized = models.BooleanField(default=False)
    cursor_observed_at = models.DateTimeField(null=True, blank=True)
    cursor_observation_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("item__share__ticker", "source", "currency", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "item",
                    "source",
                    "currency",
                    "direction",
                    "threshold",
                ),
                name="unique_watch_rule_condition",
            )
        ]
        indexes = [
            models.Index(
                fields=("enabled", "cursor_observed_at"),
                name="watch_rule_cursor_idx",
            )
        ]

    def __str__(self):
        return (
            f"{self.item.share.ticker} {self.get_direction_display()} "
            f"{self.threshold} {self.currency} ({self.source})"
        )


class PriceAlert(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="price_alerts",
    )
    rule = models.ForeignKey(
        WatchRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alerts",
    )
    observation = models.ForeignKey(
        PriceObservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alerts",
    )
    share_ticker = models.CharField(max_length=10)
    share_name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=30, decimal_places=9)
    threshold = models.DecimalField(max_digits=30, decimal_places=9)
    direction = models.CharField(max_length=3, choices=WatchRule.Direction.choices)
    source = models.CharField(max_length=20, choices=PriceObservation.Source.choices)
    currency = models.CharField(max_length=20)
    observed_at = models.DateTimeField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-observed_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("rule", "observation"),
                name="unique_alert_per_rule_observation",
            )
        ]
        indexes = [
            models.Index(
                fields=("owner", "is_read", "observed_at"),
                name="alert_owner_unread_idx",
            )
        ]

    def __str__(self):
        return f"{self.share_ticker}: {self.price} {self.currency}"


class VirtualPortfolio(models.Model):
    class ValuationSource(models.TextChoices):
        DEMO = PriceObservation.Source.DEMO, "Демонстрационные данные"
        TINKOFF = PriceObservation.Source.TINKOFF, "Tinkoff Invest"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="virtual_portfolios",
    )
    name = models.CharField(max_length=100)
    currency = models.CharField(max_length=20)
    valuation_source = models.CharField(
        max_length=20,
        choices=ValuationSource.choices,
    )
    starting_cash = models.DecimalField(max_digits=30, decimal_places=9)
    cash_balance = models.DecimalField(max_digits=30, decimal_places=9)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_virtual_portfolio_name_per_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(starting_cash__gt=0),
                name="portfolio_starting_cash_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(cash_balance__gte=0),
                name="portfolio_cash_nonnegative",
            ),
        ]

    def __str__(self):
        return self.name


class PortfolioPosition(models.Model):
    portfolio = models.ForeignKey(
        VirtualPortfolio,
        on_delete=models.CASCADE,
        related_name="positions",
    )
    share = models.ForeignKey(
        Share,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portfolio_positions",
    )
    share_ticker = models.CharField(max_length=10)
    share_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=30, decimal_places=9, default=0)
    cost_basis = models.DecimalField(max_digits=30, decimal_places=9, default=0)
    average_cost = models.DecimalField(max_digits=30, decimal_places=9, default=0)
    realized_pnl = models.DecimalField(max_digits=30, decimal_places=9, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("share_ticker", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("portfolio", "share"),
                name="unique_position_per_portfolio_share",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name="portfolio_position_quantity_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(cost_basis__gte=0),
                name="portfolio_position_cost_basis_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.portfolio.name}: {self.share_ticker}"


class PortfolioTransaction(models.Model):
    class Side(models.TextChoices):
        BUY = "buy", "Покупка (BUY)"
        SELL = "sell", "Продажа (SELL)"

    portfolio = models.ForeignKey(
        VirtualPortfolio,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    share = models.ForeignKey(
        Share,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portfolio_transactions",
    )
    share_ticker = models.CharField(max_length=10)
    share_name = models.CharField(max_length=255)
    side = models.CharField(max_length=4, choices=Side.choices)
    quantity = models.DecimalField(max_digits=30, decimal_places=9)
    execution_price = models.DecimalField(max_digits=30, decimal_places=9)
    commission = models.DecimalField(max_digits=30, decimal_places=9, default=0)
    currency = models.CharField(max_length=20)
    executed_at = models.DateTimeField()
    idempotency_key = models.UUIDField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("executed_at", "pk")
        indexes = [
            models.Index(
                fields=("portfolio", "executed_at", "id"),
                name="portfolio_tx_order_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="portfolio_tx_quantity_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(execution_price__gt=0),
                name="portfolio_tx_price_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(commission__gte=0),
                name="portfolio_tx_commission_nonnegative",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_side_display()} {self.quantity} {self.share_ticker} "
            f"@ {self.execution_price} {self.currency}"
        )
