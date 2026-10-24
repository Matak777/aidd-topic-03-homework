from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import ShareModelForm
from .models import Share
from .services.graph import get_graph
from .services.market_data import MarketDataError


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
