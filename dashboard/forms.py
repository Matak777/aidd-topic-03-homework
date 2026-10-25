from django import forms
from django.core.exceptions import ValidationError

from .models import (
    PriceObservation,
    Share,
    Watchlist,
    WatchlistItem,
    WatchRule,
)


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
