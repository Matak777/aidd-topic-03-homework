from django import forms
from django.db import transaction

from .models import (
    Portfolio,
    PriceObservation,
    Share,
    Strategy,
    Trade,
    Watchlist,
    WatchRule,
)


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


# --- Мониторинг (этап 3) ----------------------------------------------------


class WatchlistForm(forms.ModelForm):
    """Создание личного списка наблюдения (F1.1)."""

    class Meta:
        model = Watchlist
        fields = ("name",)
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if name:
            user = getattr(self, "user", None)
            if (
                user is not None
                and Watchlist.objects.filter(user=user, name=name).exists()
            ):
                raise forms.ValidationError(
                    "У вас уже есть список с таким названием."
                )
        return name


class WatchItemAddForm(forms.Form):
    """Добавление инструмента в список (без дублей, F1.1)."""

    share = forms.ModelChoiceField(
        queryset=Share.objects.all(),
        label="Инструмент",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class WatchRuleForm(forms.Form):
    """Правило мониторинга (F1.2): ≥/≤ порога, источник, валюта, вкл/откл.

    Оба направления — отдельными правилами; порог — положительный точный
    Decimal (общая политика точности этапа 2).
    """

    share = forms.ModelChoiceField(
        queryset=Share.objects.all(),
        label="Инструмент",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    direction = forms.TypedChoiceField(
        choices=WatchRule.DIRECTION_CHOICES,
        label="Направление",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    threshold = AmountField(positive=True, label="Порог (> 0)")
    source = forms.TypedChoiceField(
        choices=PriceObservation.SOURCE_CHOICES,
        label="Источник оценки",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    currency = forms.CharField(
        max_length=20,
        label="Валюта",
        help_text="Сравнение без регистра (USD/usd — одна валюта); "
        "пусто — валюта инструмента.",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    enabled = forms.BooleanField(
        label="Включено",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_currency(self):
        cur = (self.cleaned_data.get("currency") or "").strip().upper()
        return cur

    def clean(self):
        cleaned = super().clean()
        # валюта пустой формы — валюта выбранного инструмента
        share = cleaned.get("share")
        if share is not None and not cleaned.get("currency"):
            cleaned["currency"] = (share.currency or "").strip().upper()
        return cleaned

    def apply_rule(self, user, instance=None):
        """Создание/редактирование правила + пересчёт базового состояния.

        Создание/редактирование/включение — без уведомлений; актуальная
        совместимая цена становится исходным состоянием (см. сервис).
        Отключённое правило не уведомляет.
        """
        from .services import watch_monitor

        cleaned = self.cleaned_data
        threshold = watch_monitor.canonical_amount(
            cleaned["threshold"], positive=True, label="порог"
        )
        if instance is None:
            rule = WatchRule(
                user=user,
                share=cleaned["share"],
                direction=cleaned["direction"],
                threshold=threshold,
                source=cleaned["source"],
                currency=cleaned["currency"],
                enabled=bool(cleaned.get("enabled")),
            )
            rule.save()
            # создание: старая история не проигрывается (первое новое
            # наблюдение уведомляет)
            watch_monitor.reset_baseline(rule, on_change=False)
        else:
            instance.share = cleaned["share"]
            instance.direction = cleaned["direction"]
            instance.threshold = threshold
            instance.source = cleaned["source"]
            instance.currency = cleaned["currency"]
            instance.enabled = bool(cleaned.get("enabled"))
            instance.save()
            rule = instance
            # редактирование: актуальная цена — исходное состояние БЕЗ
            # уведомления; если условие уже выполнено — нужен выход
            watch_monitor.reset_baseline(rule, on_change=True)
        return rule


class StrategyForm(forms.Form):
    """Стратегия виртуальных сделок (группа 4).

    Источник должен совпадать с источником оценки портфеля, валюта —
    с валютой портфеля (без регистра). Porог/количество — точно > 0
    (AmountField positive), комиссия — ≥ 0.
    """

    portfolio = forms.ModelChoiceField(
        queryset=Portfolio.objects.none(),
        label="Портфель",
        widget=forms.Select(attrs={"class": "form-select"}),
        empty_label="—",
    )
    share = forms.ModelChoiceField(
        queryset=Share.objects.all(),
        label="Инструмент",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    name = forms.CharField(
        max_length=255,
        label="Название",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    direction = forms.TypedChoiceField(
        choices=Strategy.DIRECTION_CHOICES,
        label="Условие",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    threshold = AmountField(positive=True, label="Порог (> 0)")
    side = forms.TypedChoiceField(
        choices=Strategy.SIDE_CHOICES,
        label="Действие",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantity = AmountField(positive=True, label="Количество (> 0)")
    commission = AmountField(positive=False, label="Комиссия (>= 0)")
    source = forms.TypedChoiceField(
        choices=PriceObservation.SOURCE_CHOICES,
        label="Источник оценки",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    currency = forms.CharField(
        max_length=20,
        label="Валюта",
        required=False,
        help_text="Должна совпадать с валютой портфеля (без регистра); "
        "пусто — валюта портфеля.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    enabled = forms.BooleanField(
        label="Включено",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["portfolio"].queryset = Portfolio.objects.filter(
                user=user
            ).order_by("name", "id")

    def clean_currency(self):
        return (self.cleaned_data.get("currency") or "").strip().upper()

    def clean(self):
        cleaned = super().clean()
        portfolio = cleaned.get("portfolio")
        if portfolio is None:
            return cleaned
        # пусто — валюта портфеля
        if not cleaned.get("currency"):
            cleaned["currency"] = (
                (portfolio.base_currency or "").strip().upper()
            )
        # источник/валюта совпадают с портфелем
        if cleaned.get("source") != portfolio.valuation_source:
            self.add_error(
                "source",
                "Источник стратегии должен совпадать с источником "
                f"оценки портфеля ({portfolio.valuation_source}).",
            )
        if (cleaned.get("currency") or "").upper() != (
            portfolio.base_currency or ""
        ).strip().upper():
            self.add_error(
                "currency",
                "Валюта стратегии должна совпадать с валютой портфеля "
                f"({portfolio.base_currency.upper()}).",
            )
        share = cleaned.get("share")
        if share is not None and (
            (share.currency or "").strip().casefold()
            != (portfolio.base_currency or "").strip().casefold()
        ):
            self.add_error(
                "share",
                "Валюта инструмента должна совпадать с валютой портфеля; "
                "валютная конвертация не выполняется.",
            )
        return cleaned

    @transaction.atomic
    def apply_strategy(self, user, instance=None):
        """Создание/редактирование + пересчёт baseline (без исполнения).

        Создание: старая история не проигрывается. Редактирование:
        последняя пригодная совместимая небудущая цена — исходное
        состояние; уже выполненное условие требует выхода.
        """
        from .services import strategy_runner
        from .services.portfolio_accounting import canonical_amount

        cleaned = self.cleaned_data
        if instance is not None:
            try:
                instance = Strategy.objects.select_for_update().get(
                    pk=instance.pk, user=user, portfolio__user=user
                )
            except Strategy.DoesNotExist as error:
                raise forms.ValidationError("Стратегия недоступна.") from error
        try:
            portfolio = Portfolio.objects.select_for_update().get(
                pk=cleaned["portfolio"].pk, user=user
            )
            share = Share.objects.select_for_update().get(
                pk=cleaned["share"].pk
            )
        except (Portfolio.DoesNotExist, Share.DoesNotExist) as error:
            raise forms.ValidationError(
                "Портфель или инструмент больше недоступен."
            ) from error
        currency = cleaned["currency"].strip().upper()
        if (
            cleaned["source"] != portfolio.valuation_source
            or currency.casefold()
            != portfolio.base_currency.strip().casefold()
            or currency.casefold() != share.currency.strip().casefold()
        ):
            raise forms.ValidationError(
                "Источник и валюта должны совпадать с портфелем и инструментом."
            )
        threshold = canonical_amount(
            cleaned["threshold"], positive=True, label="порог"
        )
        quantity = canonical_amount(
            cleaned["quantity"], positive=True, label="количество"
        )
        commission = canonical_amount(
            cleaned["commission"], positive=False, label="комиссия"
        )
        fields = dict(
            portfolio=portfolio,
            share=share,
            name=cleaned["name"].strip(),
            direction=cleaned["direction"],
            threshold=threshold,
            side=cleaned["side"],
            quantity=quantity,
            commission=commission,
            source=cleaned["source"],
            currency=currency,
            enabled=bool(cleaned.get("enabled")),
        )
        if instance is None:
            strategy = Strategy(user=user, **fields)
            strategy.save()
            strategy_runner.reset_baseline(strategy, on_change=False)
        else:
            for attr, value in fields.items():
                setattr(instance, attr, value)
            instance.save()
            strategy = instance
            strategy_runner.reset_baseline(strategy, on_change=True)
        return strategy
