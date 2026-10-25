from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.conf import settings
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import ShareModelForm
from .models import PriceObservation, Share
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
