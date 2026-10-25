from django.urls import include, path

from dashboard.views import (
    ShareCreateView,
    ShareDeleteView,
    ShareDetailView,
    ShareListView,
    ShareUpdateView,
    share_price_history,
)

app_name = "dashboard"

urlpatterns = [
    path("", ShareListView.as_view(), name="index"),
    path("<int:pk>/history/", share_price_history, name="history"),
    path("<int:pk>", ShareDetailView.as_view(), name="detail"),
    path("create", ShareCreateView.as_view(), name="create"),
    path("update/<int:pk>", ShareUpdateView.as_view(), name="update"),
    path("delete/<int:pk>", ShareDeleteView.as_view(), name="delete"),
    path("api/", include("dashboard.api.urls")),
]
