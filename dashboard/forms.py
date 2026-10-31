from django import forms

from .models import Portfolio, Share, Trade


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


# --- Портфели (этап 2) -----------------------------------------------------


class AmountField(forms.CharField):
    """Текстовое поле точной Decimal-величины (деньги/количество).

    DecimalField не подходит: его NUMERIC-путь молча квантует при
    чтении из SQLite, а его валидация проверяет max_digits, не точность
    roundtrip'а. Строка передаётся в сервис как есть — записывается
    ExactAmountField'ом без изменения; допустимость значения (в т.ч.
    заявленная точность: не более 30 целых и 18 значащих дробных цифр,
    хвостовые нули не считаются) проверяет validate_metric сервиса.
    """

    def __init__(self, *, positive=True, label=None, **kwargs):
        self.positive = positive
        kwargs.setdefault("max_length", 49)
        kwargs.setdefault(
            "widget", forms.TextInput(attrs={"class": "form-control"})
        )
        super().__init__(label=label, **kwargs)

    def to_python(self, value):
        return (value or "").strip()

    def validate(self, value):
        super().validate(value)
        if value == "":
            raise forms.ValidationError("Обязательное поле.", code="required")
        from .services.portfolio_accounting import validate_metric

        try:
            validate_metric(
                value, positive=self.positive, label=self.label or "сумма"
            )
        except ValueError as e:
            raise forms.ValidationError(str(e), code="invalid_amount")


class PortfolioCreateForm(forms.ModelForm):
    """Создание виртуального портфеля (F2.1)."""

    class Meta:
        model = Portfolio
        fields = ("name", "base_currency", "valuation_source", "initial_cash")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "base_currency": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "USD"}
            ),
            "valuation_source": forms.Select(attrs={"class": "form-select"}),
        }

    initial_cash = AmountField(
        positive=True,
        label="Стартовый виртуальный баланс (> 0)",
        help_text="Положительный стартовый баланс; пополнения/вывод вне этапа.",
    )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if name:
            # user передаётся формой из view (get_form); дубли (user, name)
            # отклоняются понятным сообщением формы, без 500. У другого
            # владельца то же имя допустимо.
            user = getattr(self, "user", None)
            if (
                user is not None
                and Portfolio.objects.filter(user=user, name=name).exists()
            ):
                raise forms.ValidationError(
                    "У вас уже есть портфель с таким названием."
                )
        return name

    def clean_base_currency(self):
        cur = self.cleaned_data["base_currency"].strip().upper()
        if not cur:
            raise forms.ValidationError(
                "Укажите валюту портфеля (например, USD)."
            )
        return cur

    def clean(self):
        cleaned = super().clean()
        # канонизация стартового баланса до записи: '001000' → '1000'
        # (математически тот же Decimal), чтобы текст в БД совпадал с
        # тем, что потом проверяет CAS-обновление cash
        raw = cleaned.get("initial_cash")
        if raw:
            from .services.portfolio_accounting import canonical_amount

            try:
                cleaned["initial_cash"] = canonical_amount(
                    raw, positive=True, label="стартовый баланс"
                )
            except ValueError as e:
                self.add_error("initial_cash", forms.ValidationError(str(e)))
        return cleaned


class TradeOperationForm(forms.Form):
    """Виртуальная операция BUY/SELL (F2.2/F2.4).

    Цена — ручная цена виртуальной сделки, она не подменяет рыночные
    наблюдения; подстановка по умолчанию (если есть) делает view по
    подходящему наблюдению и показывает его источник/время рядом с формой.
    """

    SIDE_CHOICES = Trade.SIDE_CHOICES

    def __init__(self, *args, portfolio=None, **kwargs):
        super().__init__(*args, **kwargs)
        if portfolio is not None:
            # только инструменты в валюте портфеля (без регистра) —
            # несовместимая валюта отклоняется уже на уровне формы
            self.fields["share"].queryset = Share.objects.filter(
                currency__iexact=portfolio.base_currency
            )
            self.fields["share"].label_from_instance = (
                lambda s: f"{s.ticker} — {s.name} ({s.currency})"
            )

    share = forms.ModelChoiceField(
        queryset=Share.objects.none(),
        label="Инструмент",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    side = forms.TypedChoiceField(
        choices=SIDE_CHOICES,
        label="Сторона",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantity = AmountField(positive=True, label="Количество (> 0)")
    price = AmountField(positive=True, label="Цена исполнения — ручная (> 0)")
    commission = AmountField(
        positive=False,
        label="Комиссия (>= 0)",
        help_text="Неотрицательная; входит в себестоимость покупки / уменьшает реализацию продажи.",
    )
    executed_at = forms.DateTimeField(
        label="Время операции (UTC)",
        input_formats=[
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ],
        help_text="UTC; наивное время трактуется как UTC; не в будущем "
        "и не раньше последней операции портфеля.",
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"}
        ),
    )
    note = forms.CharField(
        label="Заметка",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    idempotency_key = forms.CharField(
        label="Ключ идемпотентности",
        required=False,
        max_length=128,
        help_text="Повторная отправка той же операции с тем же ключом "
        "не создаст дубль; тот же ключ с другими данными — отказ.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
