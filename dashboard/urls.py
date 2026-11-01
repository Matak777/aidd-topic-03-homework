from django.urls import include, path

from dashboard.views import (
    PortfolioCreateView,
    PortfolioDeleteView,
    PortfolioDetailView,
    PortfolioJournalView,
    PortfolioListView,
    PortfolioTradeCreateView,
    ShareCreateView,
    ShareDeleteView,
    ShareDetailView,
    ShareHistoryView,
    ShareListView,
    ShareUpdateView,
    WatchlistDetailView,
    WatchlistListView,
    WatchNotificationListView,
    WatchNotificationReadView,
    WatchNotificationsReadAllView,
    WatchRuleDeleteView,
    WatchRuleFormView,
    WatchRuleListView,
    WatchRuleToggleView,
)

app_name = "dashboard"

urlpatterns = [
    path("", ShareListView.as_view(), name="index"),
    path("<int:pk>", ShareDetailView.as_view(), name="detail"),
    path("<int:pk>/history", ShareHistoryView.as_view(), name="history"),
    path("create", ShareCreateView.as_view(), name="create"),
    path("update/<int:pk>", ShareUpdateView.as_view(), name="update"),
    path("delete/<int:pk>", ShareDeleteView.as_view(), name="delete"),
    # --- Портфели (этап 2). Владелец проверяется на сервере; изменения
    # только POST с CSRF. Чужой известный ID портфеля → 404.
    path("portfolios", PortfolioListView.as_view(), name="portfolios"),
    path(
        "portfolios/create",
        PortfolioCreateView.as_view(),
        name="portfolio-create",
    ),
    path(
        "portfolios/<int:pk>",
        PortfolioDetailView.as_view(),
        name="portfolio-detail",
    ),
    path(
        "portfolios/<int:pk>/journal",
        PortfolioJournalView.as_view(),
        name="portfolio-journal",
    ),
    path(
        "portfolios/<int:pk>/trades/new",
        PortfolioTradeCreateView.as_view(),
        name="portfolio-trade-new",
    ),
    path(
        "portfolios/<int:pk>/delete",
        PortfolioDeleteView.as_view(),
        name="portfolio-delete",
    ),
    # --- Мониторинг (этап 3). Владелец проверяется на сервере;
    # изменения только POST с CSRF.
    path("watch/lists", WatchlistListView.as_view(), name="watchlists"),
    path(
        "watch/lists/<int:pk>",
        WatchlistDetailView.as_view(),
        name="watchlist-detail",
    ),
    path("watch/rules", WatchRuleListView.as_view(), name="watch-rules"),
    path(
        "watch/rules/create",
        WatchRuleFormView.as_view(),
        name="watch-rule-create",
    ),
    path(
        "watch/rules/<int:pk>/edit",
        WatchRuleFormView.as_view(),
        name="watch-rule-edit",
    ),
    path(
        "watch/rules/<int:pk>/toggle",
        WatchRuleToggleView.as_view(),
        name="watch-rule-toggle",
    ),
    path(
        "watch/rules/<int:pk>/delete",
        WatchRuleDeleteView.as_view(),
        name="watch-rule-delete",
    ),
    path(
        "watch/notifications/read-all",
        WatchNotificationsReadAllView.as_view(),
        name="watch-notifications-read-all",
    ),
    path(
        "watch/notifications",
        WatchNotificationListView.as_view(),
        name="watch-notifications",
    ),
    path(
        "watch/notifications/<int:pk>/read",
        WatchNotificationReadView.as_view(),
        name="watch-notification-read",
    ),
    path("api/", include("dashboard.api.urls")),
]
