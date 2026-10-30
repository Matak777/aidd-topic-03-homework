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
