from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import (
    ShareModelForm,
    PortfolioTransactionForm,
    WatchlistForm,
    WatchlistItemForm,
    WatchRuleForm,
    VirtualPortfolioForm,
)
from .models import (
    PriceAlert,
    PriceObservation,
    Share,
    Watchlist,
    WatchlistItem,
    WatchRule,
    VirtualPortfolio,
)
from .services.price_alerts import initialize_new_rule_state, reset_rule_state
from .services.portfolios import execute_portfolio_transaction, value_portfolio
from .services.graph import get_graph
from .services.market_data import MarketDataError
from .services.price_history import (
    PRICE_PERIODS,
    build_price_history_graph,
    get_price_history,
)


class ShareListView(ListView):
    """Список акций."""

    model = Share
    template_name = "dashboard/list.html"
    paginate_by = 100
    ordering = ["name"]


class ShareDetailView(DetailView):
    model = Share
    template_name = "dashboard/detail.html"
    context_object_name = "share"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context["plot_div"] = get_graph(self.object)
        except MarketDataError as error:
            context["graph_error"] = str(error)
        return context


def share_price_history(request, pk):
    share = get_object_or_404(Share, pk=pk)
    period = request.GET.get("period", "30d")
    try:
        history = get_price_history(share, period)
    except ValueError:
        return HttpResponseBadRequest("Неизвестный период истории цен.")

    has_demo_observations = any(
        item.source == PriceObservation.Source.DEMO
        for item in history.observations
    )
    return render(
        request,
        "dashboard/history.html",
        {
            "share": share,
            "history": history,
            "periods": PRICE_PERIODS,
            "graph": build_price_history_graph(history.series),
            "demo_mode": settings.PRICE_DATA_MODE == "demo",
            "has_demo_observations": has_demo_observations,
            "live_available": bool(settings.TINKOFF_TOKEN),
        },
    )


@login_required
def watchlist_index(request):
    form = WatchlistForm(
        request.POST if request.method == "POST" else None,
        owner=request.user,
    )
    if request.method == "POST" and form.is_valid():
        watchlist = form.save(commit=False)
        watchlist.owner = request.user
        watchlist.save()
        return redirect("dashboard:watchlist-detail", pk=watchlist.pk)
    return render(
        request,
        "dashboard/watchlist_index.html",
        {"watchlists": Watchlist.objects.filter(owner=request.user), "form": form},
        status=400 if request.method == "POST" and form.errors else 200,
    )


@login_required
def watchlist_detail(request, pk):
    watchlist = get_object_or_404(Watchlist, pk=pk, owner=request.user)
    action = request.POST.get("action") if request.method == "POST" else None
    item_form = WatchlistItemForm(
        request.POST if action == "add-item" else None,
        watchlist=watchlist,
    )
    rule_form = WatchRuleForm(
        request.POST if action == "add-rule" else None,
    )
    if action == "add-item" and item_form.is_valid():
        item = item_form.save(commit=False)
        item.watchlist = watchlist
        item.save()
        return redirect("dashboard:watchlist-detail", pk=watchlist.pk)
    if action == "add-rule":
        item_id = request.POST.get("item_id", "")
        if not item_id.isdecimal():
            return HttpResponseBadRequest("Некорректный инструмент списка.")
        item = get_object_or_404(
            WatchlistItem,
            pk=int(item_id),
            watchlist=watchlist,
        )
        rule_form = WatchRuleForm(request.POST, item=item)
        if rule_form.is_valid():
            with transaction.atomic():
                rule = rule_form.save(commit=False)
                rule.item = item
                rule.save()
                initialize_new_rule_state(rule)
            return redirect("dashboard:watchlist-detail", pk=watchlist.pk)
    invalid_post = request.method == "POST" and (
        action not in {"add-item", "add-rule"}
        or item_form.errors
        or rule_form.errors
    )
    return render(
        request,
        "dashboard/watchlist_detail.html",
        {
            "watchlist": watchlist,
            "item_form": item_form,
            "rule_form": rule_form,
            "items": watchlist.items.select_related("share").prefetch_related("rules"),
        },
        status=400 if invalid_post else 200,
    )


@login_required
@require_POST
def watchlist_rename(request, pk):
    watchlist = get_object_or_404(Watchlist, pk=pk, owner=request.user)
    form = WatchlistForm(request.POST, instance=watchlist, owner=request.user)
    if form.is_valid():
        form.save()
        return redirect("dashboard:watchlist-detail", pk=watchlist.pk)
    return render(
        request,
        "dashboard/watchlist_detail.html",
        {
            "watchlist": watchlist,
            "item_form": WatchlistItemForm(watchlist=watchlist),
            "rule_form": WatchRuleForm(),
            "rename_form": form,
            "items": watchlist.items.select_related("share").prefetch_related("rules"),
        },
        status=400,
    )


@login_required
@require_POST
def watchlist_delete(request, pk):
    watchlist = get_object_or_404(Watchlist, pk=pk, owner=request.user)
    watchlist.delete()
    return redirect("dashboard:watchlists")


@login_required
def watch_rule_edit(request, pk):
    rule = get_object_or_404(
        WatchRule.objects.select_related("item__watchlist", "item__share"),
        pk=pk,
        item__watchlist__owner=request.user,
    )
    form = WatchRuleForm(
        request.POST if request.method == "POST" else None,
        instance=rule,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            rule = WatchRule.objects.select_for_update().get(pk=pk)
            for field in ("source", "currency", "direction", "threshold"):
                setattr(rule, field, form.cleaned_data[field])
            rule.save()
            reset_rule_state(rule)
        return redirect("dashboard:watchlist-detail", pk=rule.item.watchlist_id)
    return render(
        request,
        "dashboard/watch_rule_edit.html",
        {"rule": rule, "form": form},
        status=400 if request.method == "POST" and form.errors else 200,
    )


@login_required
@require_POST
def watch_rule_toggle(request, pk):
    rule = get_object_or_404(
        WatchRule, pk=pk, item__watchlist__owner=request.user
    )
    enabled_value = request.POST.get("enabled")
    if enabled_value not in {"true", "false"}:
        return HttpResponseBadRequest("Ожидается enabled=true или enabled=false.")
    with transaction.atomic():
        rule = WatchRule.objects.select_for_update().get(pk=pk)
        rule.enabled = enabled_value == "true"
        rule.save(update_fields=("enabled", "updated_at"))
        reset_rule_state(rule)
    return redirect("dashboard:watchlist-detail", pk=rule.item.watchlist_id)


@login_required
@require_POST
def watch_rule_delete(request, pk):
    rule = get_object_or_404(
        WatchRule, pk=pk, item__watchlist__owner=request.user
    )
    watchlist_id = rule.item.watchlist_id
    rule.delete()
    return redirect("dashboard:watchlist-detail", pk=watchlist_id)


@login_required
@require_POST
def watchlist_item_delete(request, pk):
    item = get_object_or_404(
        WatchlistItem, pk=pk, watchlist__owner=request.user
    )
    watchlist_id = item.watchlist_id
    item.delete()
    return redirect("dashboard:watchlist-detail", pk=watchlist_id)


@login_required
def price_alert_list(request):
    alerts = PriceAlert.objects.filter(owner=request.user).select_related(
        "observation"
    )
    return render(request, "dashboard/price_alerts.html", {"alerts": alerts})


@login_required
@require_POST
def price_alert_mark_read(request, pk):
    alert = get_object_or_404(PriceAlert, pk=pk, owner=request.user)
    alert.is_read = True
    alert.save(update_fields=("is_read",))
    return redirect("dashboard:price-alerts")


@login_required
def portfolio_index(request):
    form = VirtualPortfolioForm(
        request.POST if request.method == "POST" else None,
        owner=request.user,
    )
    if request.method == "POST" and form.is_valid():
        portfolio = form.save()
        return redirect("dashboard:portfolio-detail", pk=portfolio.pk)
    return render(
        request,
        "dashboard/portfolio_index.html",
        {
            "form": form,
            "portfolios": VirtualPortfolio.objects.filter(owner=request.user),
        },
        status=400 if request.method == "POST" and form.errors else 200,
    )


@login_required
def portfolio_detail(request, pk):
    portfolio = get_object_or_404(
        VirtualPortfolio,
        pk=pk,
        owner=request.user,
    )
    return render(
        request,
        "dashboard/portfolio_detail.html",
        {
            "portfolio": portfolio,
            "valuation": value_portfolio(portfolio),
            "transactions": portfolio.transactions.select_related("share"),
        },
    )


@login_required
def portfolio_transaction_create(request, pk):
    portfolio = get_object_or_404(
        VirtualPortfolio,
        pk=pk,
        owner=request.user,
    )
    form = PortfolioTransactionForm(
        request.POST if request.method == "POST" else None,
        portfolio=portfolio,
    )
    if request.method == "POST" and form.is_valid():
        try:
            record, _created = execute_portfolio_transaction(
                portfolio_id=portfolio.pk,
                share_id=form.cleaned_data["share"].pk,
                side=form.cleaned_data["side"],
                quantity=form.cleaned_data["quantity"],
                execution_price=form.cleaned_data["execution_price"],
                commission=form.cleaned_data["commission"],
                currency=form.cleaned_data["currency"],
                executed_at=form.cleaned_data["executed_at"],
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("dashboard:portfolio-detail", pk=record.portfolio_id)

    return render(
        request,
        "dashboard/portfolio_transaction_form.html",
        {"portfolio": portfolio, "form": form},
        status=400 if request.method == "POST" and form.errors else 200,
    )


class ShareCreateView(LoginRequiredMixin, CreateView):
    """Добавление инструмента."""

    template_name = "dashboard/create.html"
    form_class = ShareModelForm
    success_url = reverse_lazy("dashboard:index")

    def handle_no_permission(self):
        return redirect("dashboard:index")


class ShareUpdateView(LoginRequiredMixin, UpdateView):
    """Редактирование инструмента."""

    model = Share
    template_name = "dashboard/create.html"
    form_class = ShareModelForm
    success_url = reverse_lazy("dashboard:index")

    def handle_no_permission(self):
        return redirect("dashboard:index")


class ShareDeleteView(LoginRequiredMixin, DeleteView):
    """Удаление акции."""

    model = Share
    template_name = "dashboard/delete.html"
    success_url = reverse_lazy("dashboard:index")

    def handle_no_permission(self):
        return redirect("dashboard:index")
