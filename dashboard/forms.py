from django import forms

from .models import Share


class ShareModelForm(forms.ModelForm):
    """Инструмент (создание/редактирование через HTML)"""

    class Meta:
        model = Share
        # last_price_source здесь НЕТ намеренно: происхождение цены
        # определяет сервер по месту ввода (manual), поле из формы
        # не читается и подделать его нельзя. Изменяемое поле
        # last_price_source в fields="__all__" позволяло отправить
        # произвольный источник (tinkoff/demo) POST-ом: _post_clean
        # переписывал источник из cleaned_data поверх серверной логики.
        fields = (
            "figi",
            "isin",
            "name",
            "ticker",
            "currency",
            "uid",
            "last_price",
            "is_trend_high",
        )

    def clean(self):
        cleaned = super().clean()
        instance = self.instance
        # fix 4: у акции с накопленной историей наблюдений запрещено менять
        # идентичность (figi/uid/ticker) и валюту — это подменяет инструмент,
        # к которому относится история; обычное переименование не ломается.
        protected = instance.protected_identity_changes(cleaned)
        for field in protected:
            self.add_error(
                field,
                forms.ValidationError(
                    "У акции уже накоплена история наблюдений цен: смена "
                    "значения подменяет инструмент, к которому относится "
                    "история. Создайте новый инструмент с отдельной записью "
                    "либо удалите историю явно."
                ),
            )
        # fix 3: происхождение цены — только по факту изменения цены:
        # цена введена/изменена (в т.ч. нулевая) — manual; цена очищена —
        # источник снимается (нет цены — нет источника); цена не менялась —
        # прежний источник сохраняется.
        if "last_price" in self.changed_data:
            instance.last_price_source = (
                Share.LAST_PRICE_SOURCE_MANUAL
                if cleaned.get("last_price") is not None
                else None
            )
        return cleaned
