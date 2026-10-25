from django.contrib import admin

from dashboard.forms import ShareModelForm
from dashboard.models import PriceObservation, Share


@admin.register(Share)
class ShareAdmin(admin.ModelAdmin):
    form = ShareModelForm

    list_display = (
        "figi",
        "isin",
        "name",
        "ticker",
        "currency",
        "last_price",
        "last_price_source",
        "last_price_observed_at",
    )
    search_fields = ("figi", "isin", "name", "ticker")
    fields = (
        "figi",
        "isin",
        "name",
        "ticker",
        "currency",
        "uid",
        "last_price",
        "last_price_source",
        "last_price_observed_at",
        "is_trend_high",
    )
    readonly_fields = ("last_price_source", "last_price_observed_at")

    def save_model(self, request, obj, form, change):
        if "last_price" in form.changed_data:
            if obj.last_price is None:
                obj.last_price_source = None
                obj.last_price_observed_at = None
            else:
                obj.last_price_source = Share.PriceSource.MANUAL
                obj.last_price_observed_at = None
        super().save_model(request, obj, form, change)


@admin.register(PriceObservation)
class PriceObservationAdmin(admin.ModelAdmin):
    list_display = ("share", "price", "currency", "source", "observed_at", "received_at")
    list_filter = ("source", "currency")
    search_fields = ("share__ticker", "share__name")
    readonly_fields = ("share", "price", "currency", "source", "observed_at", "received_at")

    def has_add_permission(self, request):
        return False
