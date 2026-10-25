from django.urls import include, path

from dashboard.views import (
    ShareCreateView,
    ShareDeleteView,
    ShareDetailView,
    ShareListView,
    ShareUpdateView,
    price_alert_list,
    price_alert_mark_read,
    share_price_history,
    watch_rule_delete,
    watch_rule_edit,
    watch_rule_toggle,
    watchlist_item_delete,
    watchlist_delete,
    watchlist_detail,
    watchlist_index,
    watchlist_rename,
)

app_name = "dashboard"

urlpatterns = [
    path("", ShareListView.as_view(), name="index"),
    path("<int:pk>/history/", share_price_history, name="history"),
    path("<int:pk>", ShareDetailView.as_view(), name="detail"),
    path("create", ShareCreateView.as_view(), name="create"),
    path("update/<int:pk>", ShareUpdateView.as_view(), name="update"),
    path("delete/<int:pk>", ShareDeleteView.as_view(), name="delete"),
    path("watchlists/", watchlist_index, name="watchlists"),
    path("watchlists/<int:pk>/", watchlist_detail, name="watchlist-detail"),
    path("watchlists/<int:pk>/rename/", watchlist_rename, name="watchlist-rename"),
    path("watchlists/<int:pk>/delete/", watchlist_delete, name="watchlist-delete"),
    path(
        "watchlist-items/<int:pk>/delete/",
        watchlist_item_delete,
        name="watchlist-item-delete",
    ),
    path("watch-rules/<int:pk>/edit/", watch_rule_edit, name="watch-rule-edit"),
    path("watch-rules/<int:pk>/toggle/", watch_rule_toggle, name="watch-rule-toggle"),
    path("watch-rules/<int:pk>/delete/", watch_rule_delete, name="watch-rule-delete"),
    path("alerts/", price_alert_list, name="price-alerts"),
    path("alerts/<int:pk>/read/", price_alert_mark_read, name="price-alert-read"),
    path("api/", include("dashboard.api.urls")),
]
