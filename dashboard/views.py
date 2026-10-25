from django.contrib.auth.mixins import UserPassesTestMixin
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
from .models import Share
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
