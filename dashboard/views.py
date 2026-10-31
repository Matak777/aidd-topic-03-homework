from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404, HttpResponseServerError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
    View,
)

from .forms import ShareModelForm
from .models import Portfolio, Share
from .services.graph import get_graph


class IsUserAuthenticatedMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated

    @staticmethod
    def handle_no_permission():
        return redirect("dashboard:index")


class ShareListView(ListView):
    """Список акций"""

    model = Share
    template_name = "dashboard/list.html"
    paginate_by = 100
    ordering = ["name"]


class ShareDetailView(View):
    """Карточка акции с графиком; проблемы рыночных данных показываются
    на странице явно, не валя запрос с 500."""

    @staticmethod
    def get(request, pk):
        share = get_object_or_404(Share, id=pk)
        plot_div = None
        graph_error = None
        try:
            plot_div = get_graph(share)
        except Exception as e:
            # включая отсутствие TINKOFF_TOKEN и проблемы сети/API
            graph_error = str(e)

        return render(
            request,
            "dashboard/detail.html",
            {"plot_div": plot_div, "graph_error": graph_error, "share": share},
        )


class ShareCreateView(IsUserAuthenticatedMixin, CreateView):
    """Добавление инструмента"""

    template_name = "dashboard/create.html"
    form_class = ShareModelForm
    success_url = reverse_lazy("dashboard:index")


class ShareUpdateView(IsUserAuthenticatedMixin, UpdateView):
    """Редактирование инструмента"""

    model = Share
    template_name = "dashboard/create.html"
    form_class = ShareModelForm
    success_url = reverse_lazy("dashboard:index")


class ShareDeleteView(IsUserAuthenticatedMixin, DeleteView):
    """Удаление задачи"""

    model = Share
    template_name = "dashboard/delete.html"
    success_url = reverse_lazy("dashboard:index")


class ShareHistoryView(View):
    """История цен акции из накопленных наблюдений БД (группа 3).

    Все данные — локальные (PriceObservation), без внешних RPC; выбор
    периода через ?period=1d|7d|30d|90d|all. Ошибки явные: отсутствующий
    инструмент — 404, неверный период — 400; пустая история/одна точка/
    нулевая стартовая цена — осмысленные сообщения на странице.

    Несопоставимые серии не смешиваются (fix): наблюдения разделяются
    по (источник, валюта), каждая серия — со своими изменениями и своим
    графиком; demo-синтетика не сравнивается с рыночными котировками,
    разные валюты — друг с другом.
    """

    @staticmethod
    def get(request, pk):
        from django.http import HttpResponseBadRequest

        from dashboard.services import price_history as ph

        share = get_object_or_404(Share, id=pk)
        period = request.GET.get("period", ph.DEFAULT_PERIOD)
        try:
            days = ph.validate_period(period)
        except ValueError as e:
            # явная обработка неверного периода (не 500, не молчаливый сброс)
            return HttpResponseBadRequest(str(e))

        series = ph.history_series(share, days)
        for item in series:
            item["source_note"] = (
                "Демонстрационные данные: синтетический источник; это не "
                "живые котировки."
                if item["source"] == ph.SOURCE_DEMO
                else None
            )
        integration_note = None
        sources_used = {item["source"] for item in series}
        status = ph.integration_status_for_period(sources_used)
        if not series and status in ("demo", "tinkoff_unavailable"):
            integration_note = (
                "Наблюдений нет. "
                + (
                    "Источник PRICE_SOURCE=demo даёт синтетические цены "
                    "(не живые котировки)."
                    if status == "demo"
                    else "Интеграция с рынком недоступна: TINKOFF_TOKEN "
                    "не задан."
                )
                + " Запустите сбор: `python manage.py collect_prices`."
            )

        return render(
            request,
            "dashboard/history.html",
            {
                "share": share,
                "series": series,
                "period": period,
                "periods": list(ph.PERIOD_CHOICES),
                "integration_note": integration_note,
            },
        )


# --- Портфели (этап 2) -----------------------------------------------------


class PortfolioOwnerMixin:
    """Владелец проверяется на сервере (F2.9): чужой портфель → 404."""

    def get_portfolio(self, request, pk):
        from .models import Portfolio

        if not request.user.is_authenticated:
            return None
        return get_object_or_404(Portfolio, id=pk, user=request.user)


class PortfolioListView(ListView):
    """Список портфелей текущего пользователя."""

    template_name = "dashboard/portfolios_list.html"
    context_object_name = "portfolios"
    paginate_by = 20

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return Portfolio.objects.none()
        return Portfolio.objects.filter(user=self.request.user).order_by(
            "created_at"
        )

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dashboard:index")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["currency_of"] = {
            p.id: p.base_currency for p in ctx["object_list"]
        }
        return ctx


class PortfolioCreateView(IsUserAuthenticatedMixin, CreateView):
    """Создание виртуального портфеля (F2.1)."""

    template_name = "dashboard/portfolio_form.html"
    form_class = None  # задаётся ниже импортом форм

    def get_form_class(self):
        from .forms import PortfolioCreateForm

        return PortfolioCreateForm

    def get_form(self, form_class=None):
        # форма получает пользователя для серверной проверки
        # уникальности названия (дубли (user, name) → понятная ошибка
        # формы, не 500)
        form = super().get_form(form_class)
        form.user = self.request.user
        return form

    def get_success_url(self):
        from django.urls import reverse

        return reverse("dashboard:portfolio-detail", args=[self.object.pk])

    def form_valid(self, form):
        # объект создаётся от имени текущего пользователя, cash = стартовому
        portfolio = form.save(commit=False)
        portfolio.user = self.request.user
        portfolio.cash = portfolio.initial_cash
        try:
            portfolio.save()
        except IntegrityError:
            # страховка: гонка дублей (user, name) → понятная ошибка формы,
            # 500 не бывает; запись сохраняется только первая
            form.add_error(
                None,
                "У вас уже есть портфель с таким названием.",
            )
            return self.form_invalid(form)
        self.object = portfolio
        return redirect(self.get_success_url())


class PortfolioDetailView(PortfolioOwnerMixin, View):
    """Просмотр портфеля: итоги + состав (открытые позиции) (F2.3/F2.6)."""

    def get(self, request, pk):
        from .services.portfolio_accounting import (
            display_round,
            portfolio_summary,
            position_rows,
        )

        portfolio = self.get_portfolio(request, pk)
        if portfolio is None:
            # включая чужой портфель и анонима (F2.9)
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")
        try:
            summary = portfolio_summary(portfolio)
            rows = position_rows(portfolio)
        except ValueError as e:
            return HttpResponseServerError(str(e))

        def _round(value):
            if value is None:
                return None
            return display_round(value)

        for card in rows["cards"]:
            for key in (
                "cost",
                "unrealized_pl",
                "market_quote",
                "market_value",
            ):
                card[key] = _round(card[key])
            card["open_qty_display"] = display_round(card["open_qty"])
            card["avg_cost_display"] = (
                display_round(card["avg_cost"]) if card["avg_cost"] else None
            )
        shown = {
            "cash": _round(summary["cash"]),
            "market_value": _round(summary["market_value"]),
            "total_value": _round(summary["total_value"]),
            "realized_pl": _round(summary["realized_pl"]),
            "unrealized_pl": (
                _round(summary["unrealized_pl"])
                if summary["unrealized_pl"] is not None
                else None
            ),
            "overall_pl": _round(summary["overall_pl"]),
            "return_pct": _round(summary["return_pct"]),
            "initial_cash": _round(summary["initial_cash"]),
        }
        return render(
            request,
            "dashboard/portfolio_detail.html",
            {
                "portfolio": portfolio,
                "summary": summary,
                "shown": shown,
                "cards": rows["cards"],
                "is_demo": portfolio.valuation_source == "demo",
            },
        )


def _trade_list(trades):
    """Строки журнала для шаблона."""
    out = []
    for tr in trades:
        out.append(
            {
                "id": tr.id,
                "side": tr.side,
                "side_label": tr.get_side_display(),
                "ticker": tr.ticker,
                "figi": tr.figi,
                "quantity": tr.quantity,
                "price": tr.price,
                "commission": tr.commission,
                "currency": tr.currency,
                "executed_at": tr.executed_at,
                "note": tr.note,
            }
        )
    return out


class PortfolioJournalView(PortfolioOwnerMixin, View):
    """Журнал операций портфеля с пагинацией (F2.7/F2.10)."""

    def get(self, request, pk):
        portfolio = self.get_portfolio(request, pk)
        if portfolio is None:
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")
        trades = portfolio.trades.order_by("executed_at", "id")
        paginator = Paginator(trades, 20)
        page_obj = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            "dashboard/portfolio_journal.html",
            {
                "portfolio": portfolio,
                "trades": _trade_list(page_obj.object_list),
                "page_obj": page_obj,
                "paginator": paginator,
                "is_demo": portfolio.valuation_source == "demo",
            },
        )


class PortfolioTradeCreateView(PortfolioOwnerMixin, View):
    """Форма и исполнение виртуальной операции (GET — форма, POST — запись).

    Цена в форме — ручная цена виртуальной сделки; подстановка по
    умолчанию только из подходящего наблюдения выбранного источника
    оценки с показом источника/времени (F2.2).
    """

    def _portfolio(self, request, pk):
        if not request.user.is_authenticated:
            return None
        portfolio = self.get_portfolio(request, pk)
        return portfolio

    def get(self, request, pk):
        from .forms import TradeOperationForm
        from .models import Share
        from .services.portfolio_accounting import last_suitable_observation

        portfolio = self._portfolio(request, pk)
        if portfolio is None:
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")

        initial = {}
        suggestion = None
        share_from_url = request.GET.get("share")
        if share_from_url:
            share = Share.objects.filter(
                id=share_from_url, currency__iexact=portfolio.base_currency
            ).first()
            if share is not None:
                initial["share"] = share
                obs = last_suitable_observation(
                    share,
                    portfolio.valuation_source,
                    share.currency,
                )
                if obs is not None:
                    # подстановка из наблюдения источника оценки портфеля;
                    # источник/время показываются рядом с формой
                    initial["price"] = obs.price
                    suggestion = {
                        "price": obs.price,
                        "source": obs.source,
                        "quote_time": obs.quote_time,
                        "share": share,
                    }
                else:
                    suggestion = None
        form = TradeOperationForm(initial=initial, portfolio=portfolio)
        return render(
            request,
            "dashboard/portfolio_trade_form.html",
            {
                "portfolio": portfolio,
                "form": form,
                "suggestion": suggestion,
                "is_demo": portfolio.valuation_source == "demo",
            },
        )

    def post(self, request, pk):
        from decimal import DecimalException

        from .forms import TradeOperationForm
        from .services.portfolio_accounting import execute_trade

        portfolio = self._portfolio(request, pk)
        if portfolio is None:
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")

        form = TradeOperationForm(request.POST, portfolio=portfolio)
        if not form.is_valid():
            return self._repost_form(request, portfolio, form)
        data = form.cleaned_data
        try:
            execute_trade(
                portfolio,
                side=data["side"],
                share=data["share"],
                quantity=data["quantity"],
                price=data["price"],
                commission=data["commission"] or "0",
                executed_at=data["executed_at"],
                note=data.get("note") or "",
                idempotency_key=data.get("idempotency_key") or None,
            )
        except ValueError as e:
            # явный отказ (валидация сервиса) — форма с сообщением,
            # без частичных изменений
            form.add_error(None, str(e))
            return self._repost_form(request, portfolio, form)
        except DecimalException as e:
            # Decimal-арифметика не смогла выполниться точно/корректно —
            # явный отказ по политике точности, не 500
            form.add_error(
                None,
                "Величины или результат операции не вычисляются точно в "
                "заявленной политике точности — операция не записана "
                f"({e.__class__.__name__}).",
            )
            return self._repost_form(request, portfolio, form)
        from django.urls import reverse

        return redirect(
            reverse("dashboard:portfolio-journal", args=[portfolio.pk])
        )

    def _repost_form(self, request, portfolio, form):
        # источник оценки — фактический источник портфеля, и на
        # ошибочных ветках тоже (формовая ошибка не маркируется DEMO)
        return render(
            request,
            "dashboard/portfolio_trade_form.html",
            {
                "portfolio": portfolio,
                "form": form,
                "is_demo": portfolio.valuation_source == "demo",
            },
        )


class PortfolioDeleteView(PortfolioOwnerMixin, View):
    """Удаление пустого портфеля.

    Портфель с операциями не удаляется: Trade.portfolio — PROTECT,
    ProtectedError перехватывается и показывается понятным сообщением
    (не 500), история сохраняется (F2.7).
    """

    def get(self, request, pk):
        portfolio = self.get_portfolio(request, pk)
        if portfolio is None:
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")
        return render(
            request,
            "dashboard/portfolio_delete.html",
            {"portfolio": portfolio},
        )

    def post(self, request, pk):
        from django.db.models import ProtectedError

        portfolio = self.get_portfolio(request, pk)
        if portfolio is None:
            if not request.user.is_authenticated:
                return redirect("dashboard:index")
            raise Http404("Портфель не найден")
        try:
            portfolio.delete()
        except ProtectedError:
            # понятный отказ вместо 500: операции сохраняются
            return render(
                request,
                "dashboard/portfolio_delete.html",
                {
                    "portfolio": portfolio,
                    "protected_error": (
                        "Портфель нельзя удалить: в нём есть операции "
                        "журнала. Удаление не уничтожает финансовую "
                        "историю."
                    ),
                },
            )
        return redirect("dashboard:portfolios")
