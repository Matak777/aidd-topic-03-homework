from django import forms
from django.contrib import admin

from dashboard.models import Share

# Редактируемые поля формы admin. last_price_source здесь НЕТ намеренно:
# происхождение цены определяет сервер по факту изменения цены (см.
# ShareModelForm), из POST его подделать нельзя.
_SHARE_ADMIN_EDITABLE_FIELDS = (
    "figi",
    "isin",
    "name",
    "ticker",
    "currency",
    "uid",
    "last_price",
    "is_trend_high",
)


class ShareAdminForm(forms.ModelForm):
    """Форма admin с защитой идентичности акции с историей (fix 4).

    figi/uid/ticker у акции с накопленной историей наблюдений менять нельзя:
    это подменяет инструмент, к которому относится история. Валюта также
    заблокирована, чтобы не смешивать накопленные серии.

    last_price_source отсутствует в редактируемых полях: происхождение
    цены определяет сервер (см. ShareModelForm), из POST его подделать
    нельзя.
    """

    EDITABLE_FIELDS = _SHARE_ADMIN_EDITABLE_FIELDS

    class Meta:
        model = Share
        fields = _SHARE_ADMIN_EDITABLE_FIELDS

    def clean(self):
        cleaned = super().clean()
        protected = self.instance.protected_identity_changes(cleaned)
        for field in protected:
            self.add_error(
                field,
                forms.ValidationError(
                    "У акции уже накоплена история наблюдений цен: смена "
                    "значения подменяет инструмент, к которому относится "
                    "история. Создайте новый инструмент с отдельной записью."
                ),
            )
        # fix 3: происхождение цены — по факту изменения (как в HTML-форме):
        # изменена (в т.ч. на 0) — manual; очищена — источник снят;
        # не менялась — прежний сохранён.
        if "last_price" in self.changed_data:
            self.instance.last_price_source = (
                Share.LAST_PRICE_SOURCE_MANUAL
                if cleaned.get("last_price") is not None
                else None
            )
        return cleaned


@admin.register(Share)
class ShareAdmin(admin.ModelAdmin):
    list_display = ("figi", "isin", "name", "ticker", "currency")
    search_fields = ("figi", "isin", "name", "ticker")
    fields = (
        "figi",
        "isin",
        "name",
        "ticker",
        "currency",
        "uid",
        "last_price",
        "last_price_source",
        "is_trend_high",
    )
    readonly_fields = ("last_price_source",)
    form = ShareAdminForm
