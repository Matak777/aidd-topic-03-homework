from django.utils import timezone

from config.celery import app
from config.settings import logger
from dashboard.models import Share
from dashboard.services.market_data import get_last_prices


@app.task
def update_last_price():
    """Актуализирует последние цены всех акций каталога.

    Один gRPC-клиент и один пакетный запрос на весь каталог (вместо клиента
    и запроса на каждую акцию), чтобы задача укладывалась в интервал beat.
    Проблемы по отдельным акциям журналируются и не прерывают обход.
    """
    shares = list(Share.objects.all())
    if not shares:
        logger.info("update_last_price: no shares in catalog")
        return

    try:
        prices = get_last_prices([share.figi for share in shares])
    except Exception as e:
        # включая отсутствие TINKOFF_TOKEN: задача не падает повторно, ошибка видна в журнале
        logger.error(f"update_last_price: failed to get prices: {e}")
        return

    now = timezone.now()
    updated = 0
    for share in shares:
        item = prices.get(share.figi)
        if item is None:
            logger.error(f"No last data for share with id {share.id}")
            continue
        last_price = item[0]

        # is_trend_high: True — цена выросла, False — осталась или упала,
        # None — относительно нечего сравнивать (первая цена).
        previous = share.last_price
        if previous is None:
            is_trend_high = None
        else:
            is_trend_high = last_price > previous

        Share.objects.filter(id=share.id).update(
            last_price=last_price,
            is_trend_high=is_trend_high,
            updated_at=now,
        )
        updated += 1

    logger.info(
        f"update_last_price: updated {updated} of {len(shares)} shares"
    )
    return updated
