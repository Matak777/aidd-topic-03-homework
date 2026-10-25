from django import forms
from django.core.exceptions import ValidationError

from .models import Share


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
