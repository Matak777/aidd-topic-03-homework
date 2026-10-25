from config.celery import app
from config.settings import logger
from django.conf import settings
from dashboard.models import Share
from dashboard.services.market_data import MarketDataError, get_last_data


@app.task
def update_last_price(share_id=None):
    return _update_last_price(share_id)


def _update_last_price(share_id=None, *, stop_event=None):
    updated = 0
    shares = Share.objects.all()
    if share_id is not None:
        shares = shares.filter(pk=share_id)

    for share in shares.iterator():
        if stop_event is not None and stop_event.is_set():
            break
        try:
            last_price, observed_at = get_last_data(share)
        except MarketDataError:
            logger.exception("Skipping price update for share %s", share.pk)
            continue

        updated += 1
        if settings.PRICE_DATA_MODE == "demo":
            continue

        previous_price = share.last_price
        share.last_price = last_price
        share.last_price_source = Share.PriceSource.TINKOFF
        share.last_price_observed_at = observed_at
        share.is_trend_high = (
            last_price > previous_price if previous_price is not None else None
        )
        share.save(
            update_fields=(
                "last_price",
                "last_price_source",
                "last_price_observed_at",
                "is_trend_high",
                "updated_at",
            )
        )
    return updated
