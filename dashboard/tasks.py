from config.celery import app
from config.settings import logger
from dashboard.models import Share
from dashboard.services.market_data import MarketDataError, get_last_data


@app.task
def update_last_price():
    for share in Share.objects.all().iterator():
        try:
            last_price, _timestamp = get_last_data(share)
        except MarketDataError:
            logger.exception("Skipping price update for share %s", share.pk)
            continue

        previous_price = share.last_price
        share.last_price = last_price
        share.is_trend_high = (
            last_price > previous_price if previous_price is not None else None
        )
        share.save(update_fields=("last_price", "is_trend_high", "updated_at"))
