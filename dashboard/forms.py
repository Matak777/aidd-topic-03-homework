from uuid import uuid4
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    PriceObservation,
    PortfolioTransaction,
    Share,
    Watchlist,
    WatchlistItem,
    WatchRule,
    TradingStrategy,
    VirtualPortfolio,
)
from .services.portfolios import validate_input_amount


class ShareModelForm(forms.ModelForm):
    """Инструмент"""

    class Meta:
        model = Share
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        if self.instance.pk and any(
            field in cleaned_data
            for field in (*Share.IDENTITY_FIELDS, "currency", "last_price")
        ):
            previous = Share.objects.get(pk=self.instance.pk)
            candidate = Share.objects.get(pk=self.instance.pk)
            for field, value in cleaned_data.items():
                if hasattr(candidate, field):
                    setattr(candidate, field, value)
            try:
                candidate.apply_update_policy(
                    previous,
                    explicit_price="last_price" in self.changed_data,
                )
            except ValidationError as error:
                for field, messages in error.message_dict.items():
                    for message in messages:
                        self.add_error(field, message)
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.pk:
            previous = Share.objects.get(pk=instance.pk)
            instance.apply_update_policy(
                previous,
                explicit_price="last_price" in self.changed_data,
            )
        elif "last_price" in self.changed_data:
            instance.last_price_source = (
                Share.PriceSource.MANUAL if instance.last_price is not None else None
            )
            instance.last_price_observed_at = None
            instance.is_trend_high = None
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class WatchlistForm(forms.ModelForm):
    class Meta:
        model = Watchlist
        fields = ("name",)

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        duplicate = Watchlist.objects.filter(owner=self.owner, name=name)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise ValidationError("У вас уже есть список с таким названием.")
        return name


class WatchlistItemForm(forms.ModelForm):
    class Meta:
        model = WatchlistItem
        fields = ("share",)

    def __init__(self, *args, watchlist, **kwargs):
        self.watchlist = watchlist
        super().__init__(*args, **kwargs)
        existing_share_ids = watchlist.items.values_list("share_id", flat=True)
        self.fields["share"].queryset = Share.objects.exclude(
            pk__in=existing_share_ids
        ).order_by("ticker")


class WatchRuleForm(forms.ModelForm):
    source = forms.ChoiceField(choices=PriceObservation.Source.choices)
    currency = forms.ChoiceField()

    class Meta:
        model = WatchRule
        fields = ("source", "currency", "direction", "threshold")

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item or (self.instance.item if self.instance.pk else None)
        currencies = set(
            value.lower()
            for value in Share.objects.values_list("currency", flat=True)
            if value
        )
        currencies.update(
            value.lower()
            for value in PriceObservation.objects.values_list(
                "currency", flat=True
            ).distinct()
            if value
        )
        self.fields["currency"].choices = [
            (value, value.upper()) for value in sorted(currencies)
        ]

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().lower()

    def clean_threshold(self):
        threshold = self.cleaned_data["threshold"]
        if threshold < 0:
            raise ValidationError("Пороговая цена не может быть отрицательной.")
        return threshold

    def clean(self):
        cleaned_data = super().clean()
        source = cleaned_data.get("source")
        currency = cleaned_data.get("currency")
        direction = cleaned_data.get("direction")
        threshold = cleaned_data.get("threshold")
        if self.item and all(
            value is not None for value in (source, currency, direction, threshold)
        ):
            duplicate = WatchRule.objects.filter(
                item=self.item,
                source=source,
                currency__iexact=currency,
                direction=direction,
                threshold=threshold,
            ).exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise ValidationError("Такое правило уже есть в этом списке.")
        return cleaned_data


class VirtualPortfolioForm(forms.ModelForm):
    class Meta:
        model = VirtualPortfolio
        fields = ("name", "currency", "valuation_source", "starting_cash")
        widgets = {
            "starting_cash": forms.NumberInput(attrs={"step": "0.000000001"}),
        }

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if VirtualPortfolio.objects.filter(owner=self.owner, name=name).exists():
            raise ValidationError("Портфель с таким названием уже существует.")
        return name

    def clean_currency(self):
        currency = self.cleaned_data["currency"].strip()
        if not currency.isalpha() or not 3 <= len(currency) <= 20:
            raise ValidationError("Укажите код валюты буквами (например, USD).")
        return currency.upper()

    def clean_starting_cash(self):
        cash = validate_input_amount(self.cleaned_data["starting_cash"])
        if cash <= 0:
            raise ValidationError("Стартовый виртуальный баланс должен быть больше нуля.")
        return cash

    def save(self, commit=True):
        portfolio = super().save(commit=False)
        portfolio.owner = self.owner
        portfolio.cash_balance = self.cleaned_data["starting_cash"]
        if commit:
            portfolio.save()
        return portfolio


class PortfolioTransactionForm(forms.Form):
    share = forms.ModelChoiceField(queryset=Share.objects.none(), label="Инструмент")
    side = forms.ChoiceField(
        choices=PortfolioTransaction.Side.choices,
        label="Тип виртуальной операции",
    )
    quantity = forms.DecimalField(
        max_digits=30,
        decimal_places=9,
        min_value=Decimal("0.000000001"),
        label="Количество",
        widget=forms.NumberInput(attrs={"step": "0.000000001"}),
    )
    execution_price = forms.DecimalField(
        max_digits=30,
        decimal_places=9,
        min_value=Decimal("0.000000001"),
        label="Ручная цена виртуальной сделки",
        widget=forms.NumberInput(attrs={"step": "0.000000001"}),
    )
    commission = forms.DecimalField(
        max_digits=30,
        decimal_places=9,
        min_value=Decimal("0"),
        initial=Decimal("0"),
        label="Комиссия",
        widget=forms.NumberInput(attrs={"step": "0.000000001"}),
    )
    currency = forms.CharField(max_length=20, label="Валюта операции")
    executed_at = forms.DateTimeField(
        label="Время операции",
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, portfolio, **kwargs):
        self.portfolio = portfolio
        kwargs.setdefault(
            "initial",
            {
                "currency": portfolio.currency,
                "executed_at": timezone.localtime(),
                "idempotency_key": uuid4(),
            },
        )
        super().__init__(*args, **kwargs)
        self.fields["share"].queryset = Share.objects.order_by("ticker")
        self.fields["currency"].initial = portfolio.currency
        self.fields["currency"].widget.attrs["readonly"] = True

    def clean_currency(self):
        currency = self.cleaned_data["currency"].strip().upper()
        if currency.casefold() != self.portfolio.currency.casefold():
            raise ValidationError(
                f"Валюта операции должна совпадать с валютой портфеля "
                f"({self.portfolio.currency})."
            )
        return currency

    def clean_quantity(self):
        return validate_input_amount(self.cleaned_data["quantity"])

    def clean_execution_price(self):
        return validate_input_amount(self.cleaned_data["execution_price"])

    def clean_commission(self):
        return validate_input_amount(self.cleaned_data["commission"])


class TradingStrategyForm(forms.ModelForm):
    portfolio = forms.ModelChoiceField(
        queryset=VirtualPortfolio.objects.none(),
        label="Личный портфель",
    )
    share = forms.ModelChoiceField(
        queryset=Share.objects.order_by("ticker"),
        label="Инструмент",
    )

    class Meta:
        model = TradingStrategy
        fields = (
            "name",
            "portfolio",
            "share",
            "source",
            "currency",
            "direction",
            "threshold",
            "action",
            "quantity",
            "commission",
        )
        widgets = {
            "threshold": forms.NumberInput(attrs={"step": "0.000000001"}),
            "quantity": forms.NumberInput(attrs={"step": "0.000000001"}),
            "commission": forms.NumberInput(attrs={"step": "0.000000001"}),
        }

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        self.fields["portfolio"].queryset = VirtualPortfolio.objects.filter(
            owner=owner
        ).order_by("name", "pk")

    def clean_threshold(self):
        threshold = validate_input_amount(self.cleaned_data["threshold"])
        if threshold < 0:
            raise ValidationError("Порог не может быть отрицательным.")
        return threshold

    def clean_quantity(self):
        quantity = validate_input_amount(self.cleaned_data["quantity"])
        if quantity <= 0:
            raise ValidationError("Количество должно быть положительным.")
        return quantity

    def clean_commission(self):
        commission = validate_input_amount(self.cleaned_data["commission"])
        if commission < 0:
            raise ValidationError("Комиссия не может быть отрицательной.")
        return commission

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().upper()

    def clean(self):
        cleaned_data = super().clean()
        portfolio = cleaned_data.get("portfolio")
        share = cleaned_data.get("share")
        source = cleaned_data.get("source")
        currency = cleaned_data.get("currency")
        if portfolio and source and source != portfolio.valuation_source:
            self.add_error(
                "source",
                "Источник стратегии должен совпадать с источником оценки портфеля.",
            )
        if portfolio and currency and currency.casefold() != portfolio.currency.casefold():
            self.add_error(
                "currency",
                "Валюта стратегии должна совпадать с валютой портфеля.",
            )
        if share and currency and currency.casefold() != share.currency.casefold():
            self.add_error(
                "currency",
                "Валюта стратегии должна совпадать с валютой инструмента.",
            )
        return cleaned_data

    def save(self, commit=True):
        strategy = super().save(commit=False)
        strategy.owner = self.owner
        strategy.share_ticker = strategy.share.ticker
        strategy.share_name = strategy.share.name
        if commit:
            strategy.save()
        return strategy
