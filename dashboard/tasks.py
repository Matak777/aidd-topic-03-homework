from config.celery import app
from config.settings import logger
from dashboard.models import Share
from dashboard.services import price_history


@app.task
def update_last_price():
    """Актуализирует цены всех акций каталога.

    Один источник данных (настройка PRICE_SOURCE) и один общий путь записи
    с локальным коллектором (apply_prices): обновляется и текущий кеш
    Share.last_price (+ last_price_source: tinkoff/demo — источник честно
    зафиксирован, demo не выдаётся за рынок), и накопленная история
    PriceObservation. Celery остаётся опциональным: задача работает и без
    Redis, локальный сбор реализован management-командой (FEATURES.md §6).
    """
    shares = list(Share.objects.all())
    if not shares:
        logger.info("update_last_price: no shares in catalog")
        return

    source = price_history.resolve_source()
    try:
        prices = price_history.fetch_prices(source, shares)
    except Exception as e:
        # включая отсутствие TINKOFF_TOKEN: задача не падает повторно, ошибка
        # видна в журнале
        logger.error(f"update_last_price: failed to get prices: {e}")
        return

    processed, created = price_history.apply_prices(prices, shares, source)
    logger.info(
        f"update_last_price: processed {processed} of {len(shares)} shares,"
        f" new observations {created}"
    )
    return processed
