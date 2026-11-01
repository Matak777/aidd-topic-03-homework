from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404, HttpResponseServerError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
    View,
)

from .forms import ShareModelForm
from .models import (
    Portfolio,
    Share,
    Watchlist,
)
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


# --- Мониторинг (этап 3) ----------------------------------------------------


def _watch_auth(request):
    """Аноним → redirect на главную; авторизованный — продолжаем."""
    if not request.user.is_authenticated:
        return redirect("dashboard:index")
    return None


class WatchlistListView(View):
    """Список личных списков наблюдения + создание (F1.1)."""

    template_name = "dashboard/watch_lists.html"

    def _context(self, request, form):
        from django.db.models import Count

        from .forms import WatchlistForm
        from .models import Watchlist

        if form is None:
            form = WatchlistForm()
        lists = (
            Watchlist.objects.filter(user=request.user)
            .annotate(items_count=Count("items"))
            .order_by("name", "id")
        )
        return {
            "lists": lists,
            "form": form,
            "form_user": request.user,
        }

    def get(self, request):
        return _watch_auth(request) or render(
            request, self.template_name, self._context(request, None)
        )

    def post(self, request):
        from .forms import WatchlistForm
        from .models import Watchlist

        guard = _watch_auth(request)
        if guard:
            return guard
        form = WatchlistForm(request.POST)
        form.user = request.user
        if form.is_valid():
            watchlist = form.save(commit=False)
            watchlist.user = request.user
            try:
                watchlist.save()
            except IntegrityError:
                # страховка гонки дублей (user, name): понятная ошибка
                form.add_error(
                    None, "У вас уже есть список с таким названием."
                )
            else:
                return redirect("dashboard:watchlist-detail", watchlist.pk)
        return render(
            request,
            self.template_name,
            self._context(request, form),
        )


class WatchlistDetailView(View):
    """Карточка списка: добавление/удаление инструментов (F1.1)."""

    template_name = "dashboard/watch_list_detail.html"

    def _watchlist(self, request, pk):
        if not request.user.is_authenticated:
            return None
        return get_object_or_404(Watchlist, id=pk, user=request.user)

    def get(self, request, pk):
        guard = _watch_auth(request)
        if guard:
            return guard
        watchlist = self._watchlist(request, pk)
        if watchlist is None:
            return redirect("dashboard:index")
        from .forms import WatchItemAddForm

        return render(
            request,
            self.template_name,
            {
                "watchlist": watchlist,
                "form": WatchItemAddForm(),
                "error": None,
            },
        )

    def post(self, request, pk):
        guard = _watch_auth(request)
        if guard:
            return guard
        watchlist = self._watchlist(request, pk)
        if watchlist is None:
            # чужой или неизвестный список → 404 (без изменений)
            raise Http404("Список наблюдения не найден")
        from .forms import WatchItemAddForm
        from .models import WatchlistItem

        action = request.POST.get("action", "share-add")
        error = None
        if action == "share-add":
            form = WatchItemAddForm(request.POST)
            if not form.is_valid():
                error = "Выберите инструмент из каталога."
            else:
                share = form.cleaned_data["share"]
                item, created = WatchlistItem.objects.get_or_create(
                    watchlist=watchlist, share=share
                )
                if created:
                    # Post/Redirect/Get после успешного изменения
                    return redirect("dashboard:watchlist-detail", watchlist.pk)
                # повторное добавление — без дубля и без 500
                error = f"{share.ticker} уже есть в этом списке " "наблюдения."
        elif action == "share-remove":
            # валидация ввода до обращения к БД: некорректный ID
            # (abc/пусто) — понятное сообщение без изменений и без 500
            raw = (request.POST.get("share") or "").strip()
            if not raw.isdigit():
                error = "Некорректный инструмент для удаления."
            else:
                WatchlistItem.objects.filter(
                    watchlist=watchlist, share_id=int(raw)
                ).delete()
                return redirect("dashboard:watchlist-detail", watchlist.pk)
        elif action == "list-delete":
            watchlist.delete()
            return redirect("dashboard:watchlists")
        else:
            error = "Неизвестное действие."
        from .forms import WatchItemAddForm as F

        return render(
            request,
            self.template_name,
            {
                "watchlist": watchlist,
                "form": F(),
                "error": error,
            },
        )


class WatchRuleListView(View):
    """Список правил мониторинга; вкл/откл/удаление — POST (F1.2/F1.3)."""

    template_name = "dashboard/watch_rules.html"

    def get(self, request):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchRule

        rules = (
            WatchRule.objects.filter(user=request.user)
            .select_related("share")
            .order_by("id")
        )
        return render(request, self.template_name, {"rules": rules})


class WatchRuleFormView(View):
    """Создание/редактирование правила (F1.2). Форма общая."""

    template_name = "dashboard/watch_rule_form.html"
    instance = None

    def _rule(self, request, pk):
        from .models import WatchRule

        return get_object_or_404(WatchRule, id=pk, user=request.user)

    def _initial(self, rule):
        return {
            "share": rule.share,
            "direction": rule.direction,
            "threshold": rule.threshold,
            "source": rule.source,
            "currency": rule.currency,
            "enabled": rule.enabled,
        }

    def get(self, request, pk=None):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .forms import WatchRuleForm

        instance = None
        if pk is not None:
            instance = self._rule(request, pk)
            form = WatchRuleForm(initial=self._initial(instance))
        else:
            form = WatchRuleForm()
        return render(
            request,
            self.template_name,
            {"form": form, "rule": instance},
        )

    def post(self, request, pk=None):
        from .forms import WatchRuleForm

        guard = _watch_auth(request)
        if guard:
            return guard
        instance = None
        if pk is not None:
            instance = self._rule(request, pk)
        form = WatchRuleForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"form": form, "rule": instance},
            )
        form.apply_rule(request.user, instance=instance)
        return redirect("dashboard:watch-rules")


class WatchRuleToggleView(View):
    """Вкл/откл правила (POST): включение пересчитывает исходное
    состояние без уведомления; отключённое правило не уведомляет."""

    def post(self, request, pk):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchRule
        from .services import watch_monitor

        rule = get_object_or_404(WatchRule, id=pk, user=request.user)
        if not rule.enabled:
            rule.enabled = True
            rule.save()
            # включение: актуальная цена — исходное состояние без
            # уведомления; если условие выполнено — нужен выход
            watch_monitor.reset_baseline(rule, on_change=True)
        else:
            rule.enabled = False
            rule.save()
        return redirect("dashboard:watch-rules")


class WatchRuleDeleteView(View):
    """Удаление правила (POST): сработавшие уведомления переживают его
    (снимки; SET_NULL), состояние не переписывается."""

    def post(self, request, pk):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchRule

        rule = get_object_or_404(WatchRule, id=pk, user=request.user)
        rule.delete()
        return redirect("dashboard:watch-rules")


class WatchNotificationListView(View):
    """Список уведомлений пользователя (F1.3) с DEMO-бейджами."""

    template_name = "dashboard/watch_notifications.html"
    paginate_by = 50

    def get(self, request):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchEvent

        events = WatchEvent.objects.filter(user=request.user)
        paginator = Paginator(events, self.paginate_by)
        page_obj = paginator.get_page(request.GET.get("page"))
        return render(
            request,
            self.template_name,
            {"page_obj": page_obj},
        )


class WatchNotificationReadView(View):
    """Отметка уведомления прочитанным (POST, только владелец)."""

    def post(self, request, pk):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchEvent

        event = get_object_or_404(WatchEvent, id=pk, user=request.user)
        if not event.is_read:
            event.is_read = True
            event.read_at = timezone.now()
            event.save()
        return redirect("dashboard:watch-notifications")


class WatchNotificationsReadAllView(View):
    """Отметить все непрочитанные уведомления пользователя (POST)."""

    def post(self, request):
        guard = _watch_auth(request)
        if guard:
            return guard
        from .models import WatchEvent

        WatchEvent.objects.filter(user=request.user, is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
        return redirect("dashboard:watch-notifications")


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
