"""Накопление и анализ истории рыночных цен (группа 3, FEATURES.md).

Ключевые решения:
- точная цена: Decimal по exact units/nano из gRPC Quotation — без float;
- дедупликация повторной доставки: уникальность (share, source, quote_time);
  get_or_create делает повторную запись идемпотентной, а одинаковая цена
  в другое время сохраняется (quote_time различен);
- источники данных явные: "tinkoff" — реальный API через существующий слой
  market_data (с gRPC-дедлайном); "demo" — только явно включаемый
  синтетический режим для демонстрации без токена, в интерфейсе помечается
  как демонстрационные данные;
- расчёты для страницы истории и график строятся из БД, без внешних RPC.
"""

import math
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from config.settings import logger
from dashboard.models import PriceObservation, Share

SOURCE_TINKOFF = PriceObservation.SOURCE_TINKOFF
SOURCE_DEMO = PriceObservation.SOURCE_DEMO

# Периоды просмотра истории (ключ — значение параметра ?period=)
PERIOD_CHOICES = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}
DEFAULT_PERIOD = "30d"


def validate_period(value) -> int | None:
    """Ключ периода → число дней; неизвестный период — ValueError."""
    try:
        return PERIOD_CHOICES[value]
    except KeyError:
        raise ValueError(
            f"Неверный период: {value!r}. Доступны: "
            f"{', '.join(PERIOD_CHOICES)}."
        ) from None


def validate_interval(value, description: str = "интервал") -> float:
    """Интервал коллектора — конечное положительное число секунд."""
    try:
        interval = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{description} должен быть числом секунд, получено {value!r}"
        ) from None
    if math.isnan(interval) or math.isinf(interval) or interval <= 0:
        raise ValueError(
            f"{description} должен быть конечным положительным числом секунд "
            f"(ноль, отрицательные, NaN, бесконечность запрещены), "
            f"получено {value!r}"
        )
    return interval


def resolve_source(override: str | None = None) -> str:
    """Источник цен: явный аргумент или настройка PRICE_SOURCE."""
    from django.conf import settings

    source = (
        override or getattr(settings, "PRICE_SOURCE", SOURCE_TINKOFF)
    ).lower()
    if source not in (SOURCE_TINKOFF, SOURCE_DEMO):
        raise ValueError(
            f"Неизвестный источник цен: {source!r}; "
            f"разрешены {SOURCE_TINKOFF!r} и {SOURCE_DEMO!r}"
        )
    return source


# --- Точное преобразование цены ---


def quotation_to_decimal(quotation) -> Decimal:
    """Quotation (units + nano) → Decimal без float-арифметики.

    Реализация живёт рядом с gRPC-слоем (market_data); здесь — тонкий
    реэкспорт, чтобы доменные модули импортировали одно имя.
    """
    from dashboard.services.market_data import quotation_to_decimal as impl

    return impl(quotation)


# --- Запись наблюдений ---


def record_observations(price_by_figi, shares, source: str) -> int:
    """Записывает наблюдения с дедупликацией повторной доставки.

    price_by_figi: {figi: (Decimal price, quote_time)}.
    Возвращает число реально созданных строк.

    Цена сохраняется как точная десятичная строка (TEXT-колонка, см. модель):
    NUMERIC-представление на SQLite молча округляет; Decimal-вход
    (quotation_to_decimal) обязателен для точности, прочие типы конвертируются
    через str.
    """
    if source not in (SOURCE_TINKOFF, SOURCE_DEMO):
        raise ValueError(f"Неизвестный источник наблюдений: {source!r}")
    created = 0
    for share in shares:
        item = price_by_figi.get(share.figi)
        if item is None:
            continue
        price, quote_time = item
        if price is None or quote_time is None:
            continue
        # Точный путь — Decimal (quotation_to_decimal). Прочие входы
        # (в основном из тестов и совместимости) конвертируются через str:
        # точность гарантируется только Decimal-путём.
        if not isinstance(price, Decimal):
            price = Decimal(str(price))
        _, was_created = PriceObservation.objects.get_or_create(
            share=share,
            source=source,
            quote_time=quote_time,
            defaults={
                # format 'f' — без экспоненциальной записи ('1E-9' и т.п.)
                "price": format(price, "f"),
                "currency": share.currency,
            },
        )
        if was_created:
            created += 1
    return created


def apply_prices(prices, shares, source: str, now=None) -> tuple:
    """Единый путь получения цены: текущий кеш (Share) + история.

    Используется и задачей Celery update_last_price, и локальным
    коллетором collect_cycle — оба пути дают согласованное состояние:
    Share.last_price обновляется вместе с накопленной историей, источник
    цены фиксируется в last_price_source (demo не выдаётся за рынок).

    Запись кеша/источника/признака тренда/времени и соответствующего
    наблюдения — атомарна на инструмент (transaction.atomic): при ошибке
    записи наблюдения откатывается и кеш, иначе текущая цена оказалась бы
    «новой» без соответствующей точки истории. Ошибка поднимается наружу —
    вызывающий решает, логировать и продолжать ли цикл.

    Возвращает (обработано акций, создано новых наблюдений): повторная
    доставка, не создавшая строку, не считается новым наблюдением.
    После записи наблюдений того же пути вызывается автоматическая
    проверка правил мониторинга (watch_monitor, этап 3) — на обоих
    вызовах (локальный коллектор, задача Celery).
    """
    from django.db import transaction

    source = resolve_source(source)
    if now is None:
        now = timezone.now()
    updated = 0
    created_observations = 0
    for share in shares:
        item = prices.get(share.figi)
        if item is None:
            continue
        price_exact, quote_time = item
        # last_price остаётся float-полем совместимости (существующий
        # интерфейс); точная величина хранится в наблюдениях.
        last_price = float(price_exact)
        previous = share.last_price
        if previous is None:
            is_trend_high = None
        else:
            is_trend_high = last_price > previous
        with transaction.atomic():
            # время котировки — от источника; получателем оно не переписывается
            created_observations += record_observations(
                {share.figi: (price_exact, quote_time or now)},
                [share],
                source,
            )
            Share.objects.filter(id=share.id).update(
                last_price=last_price,
                is_trend_high=is_trend_high,
                updated_at=now,
                last_price_source=source,
            )
        # Автоматическая проверка правил мониторинга (этап 3): общий путь
        # записи наблюдений — локальный коллектор и Celery-задача вызывают
        # её одинаково, Redis не обязателен. Сбой обработки правила
        # журнализуется и не прерывает остальные правила и цикл.
        from dashboard.services import watch_monitor

        try:
            watch_monitor.process_share(share, now=now)
        except Exception as e:
            logger.error(
                f"apply_prices: watch rules for {share.figi} failed: {e}"
            )
        updated += 1
    return updated, created_observations


# --- Источники цен ---


def demo_prices(shares) -> dict:
    """Синтетические цены для демо-режима (явно, только по настройке).

    Детерминировано от времени и акции: базовая цена — последняя сохранённая
    (либо 100), плюс небольшое синусоидальное смещение. Цены гарантированно
    положительны, возвращаются Decimal'ом с временем котировки "сейчас".
    """
    quote_time = timezone.now()
    result = {}
    for share in shares:
        base = (
            share.last_price
            if share.last_price and share.last_price > 0
            else 100.0
        )
        phase = quote_time.timestamp() / 60.0 + share.id
        price = base * (1.0 + 0.01 * math.sin(phase))
        result[share.figi] = (
            (Decimal(str(price))).quantize(Decimal("0.0001")),
            quote_time,
        )
    return result


def tinkoff_prices(shares, timeout_s=None) -> dict:
    """Реальные цены через существующий слой market_data.

    get_last_prices ограничен gRPC-дедлайном; без токена поднимает
    TinkoffTokenMissing (честный статус недоступности).
    """
    from dashboard.services.market_data import get_last_prices

    figi_list = [share.figi for share in shares]
    return get_last_prices(figi_list, timeout_s=timeout_s)


def fetch_prices(source: str, shares, timeout_s=None) -> dict:
    """Выбор источника: {figi: (Decimal, quote_time)}."""
    source = resolve_source(source)
    if source == SOURCE_DEMO:
        return demo_prices(shares)
    return tinkoff_prices(shares, timeout_s=timeout_s)


def collect_cycle(source: str | None = None, timeout_s=None):
    """Один цикл сбора: получить цены каталога и применить их.

    same путь, что и задача Celery (apply_prices): обновляет и текущий
    кеш Share.last_price (+ источник), и накопленную историю — два
    сторонних механизма не расходятся. Ошибка источника (включая
    отсутствие токена) поднимается наружу — вызывающий цикл
    (management command, Celery, тест) решает, продолжать ли.
    Возвращает (создано новых наблюдений, обработано акций); повторная
    доставка, не создавшая строку, не считается новым наблюдением.
    """
    source = resolve_source(source)
    shares = list(Share.objects.all())
    if not shares:
        return 0, 0
    prices = fetch_prices(source, shares, timeout_s=timeout_s)
    processed, created = apply_prices(prices, shares, source)
    logger.info(
        f"collect_cycle[{source}]: processed {processed} of {len(shares)},"
        f" new observations {created}"
    )
    return created, processed


# --- История для страницы ---


def observations_for_period(share, days: int | None):
    """Наблюдения акции за период (days=None — вся история), по возрастанию.

    Период ограничен текущим временем сверху (quote_time <= now): точка
    «из будущего» (некорректное время котировки источника) не должна
    входить ни в один период и менять последнее наблюдение/проценты.
    """
    now = timezone.now()
    qs = share.price_observations.filter(quote_time__lte=now).order_by(
        "quote_time", "id"
    )
    if days is not None:
        qs = qs.filter(quote_time__gte=now - timedelta(days=days))
    return qs


def history_series(share, days: int | None) -> list[dict]:
    """Наблюдения периода, разделённые на несопоставимые серии.

    Серии различаются по (источник, валюта): сравнивать demo-синтетику с
    рыночными котировками и значения в разных валютах нельзя, смешивать их
    в одну линию/расчёт запрещено (фиктивные проценты). Для каждой серии
    считаются собственные изменения и строится собственный график.
    Возвращает список dicts: source, currency, observations, summary, graph.
    """
    observations = list(observations_for_period(share, days))
    series: dict[tuple[str, str], list] = {}
    for obs in observations:
        series.setdefault((obs.source, obs.currency), []).append(obs)
    result = []
    for (source, currency), rows in sorted(
        series.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        result.append(
            {
                "source": source,
                "currency": currency,
                "observations": rows,
                "summary": summarize(rows),
                "graph": build_history_graph(rows),
            }
        )
    return result


def summarize(observations):
    """Абсолютное/процентное изменение за период (Decimal; без float).

    Случаи: пустая история, одна точка (изменение не определено),
    нулевая начальная цена (процент не определён, абсолютное — есть).
    Принимает queryset, менеджер или готовый список наблюдений.
    """
    if not isinstance(observations, (list, tuple)):
        observations = list(observations.all())
    summary = {
        "count": len(observations),
        "first": None,
        "last": None,
        "change_abs": None,
        "change_pct": None,
        "zero_first": False,
    }
    if not observations:
        return summary
    first, last = observations[0], observations[-1]
    summary["first"], summary["last"] = first, last
    if len(observations) == 1:
        return summary
    change_abs = last.price_decimal - first.price_decimal
    summary["change_abs"] = change_abs
    if first.price_decimal == 0:
        summary["zero_first"] = True
        return summary
    summary["change_pct"] = (
        change_abs / first.price_decimal * Decimal("100")
    ).quantize(Decimal("0.0001"))
    return summary


def build_history_graph(observations):
    """HTML-график Plotly из наблюдений БД (внешних RPC нет).

    None — если точек меньше двух (страница показывает причину).
    """
    observations = list(observations)
    if len(observations) < 2:
        return None
    from plotly.graph_objs import Scatter
    from plotly.offline import plot

    x_data = [obs.quote_time for obs in observations]
    # float — только для отрисовки у Plotly; расчёты — Decimal (summarize)
    y_data = [float(obs.price_decimal) for obs in observations]
    return plot(
        [Scatter(x=x_data, y=y_data, mode="lines+markers", name="цена")],
        output_type="div",
        show_link=False,
        link_text="",
    )


def integration_status_for_period(sources_used) -> str:
    """Честный статус источника для страницы истории.

    sources_used — множество источников наблюдений отображаемого периода.
    """
    from django.conf import settings

    sources_used = set(sources_used)
    if SOURCE_DEMO in sources_used:
        return "demo"
    if not getattr(settings, "TINKOFF_TOKEN", ""):
        return "tinkoff_unavailable"
    if SOURCE_TINKOFF in sources_used:
        return "tinkoff"
    return "no_data"
