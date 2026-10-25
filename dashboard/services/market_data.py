import logging
import math
from collections import namedtuple
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal

import grpc
from django.conf import settings
from django.utils import timezone

from dashboard.models import PriceObservation

logger = logging.getLogger("main")


class MarketDataError(RuntimeError):
    """Raised when market data cannot be obtained from the provider."""


def _quotation_to_decimal(quotation):
    return Decimal(quotation.units) + Decimal(quotation.nano).scaleb(-9)


def record_price_observation(share, price, observed_at, source):
    if not isinstance(observed_at, datetime):
        raise MarketDataError("Источник цены не передал время котировки.")
    if not timezone.is_aware(observed_at):
        observed_at = timezone.make_aware(observed_at, datetime_timezone.utc)

    observation, _created = PriceObservation.objects.get_or_create(
        share=share,
        source=source,
        observed_at=observed_at,
        defaults={
            "price": price,
            "currency": share.currency,
            "received_at": timezone.now(),
        },
    )
    return observation


class _ClientCallDetails(
    namedtuple(
        "_ClientCallDetails",
        ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
    ),
    grpc.ClientCallDetails,
):
    pass


class RpcDeadlineInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, timeout):
        self.timeout = timeout

    def intercept_unary_unary(self, continuation, client_call_details, request):
        configured_timeout = client_call_details.timeout
        timeout = self.timeout
        if configured_timeout is not None and math.isfinite(configured_timeout):
            timeout = min(configured_timeout, self.timeout)
        call_details = _ClientCallDetails(
            method=client_call_details.method,
            timeout=timeout,
            metadata=client_call_details.metadata,
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
            compression=client_call_details.compression,
        )
        return continuation(call_details, request)


def _provider_error(error, operation, message):
    code = getattr(error, "code", None)
    if callable(code):
        code = code()

    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        logger.warning(
            "Tinkoff API %s request exceeded the %.3g-second deadline",
            operation,
            settings.TINKOFF_RPC_TIMEOUT_SECONDS,
        )
        return MarketDataError(
            f"Истекло время ожидания ответа Tinkoff API при {operation} "
            f"({settings.TINKOFF_RPC_TIMEOUT_SECONDS:g} сек.)."
        )

    logger.exception("Tinkoff API failed during %s", operation)
    return MarketDataError(message)


def get_client():
    if not settings.TINKOFF_TOKEN:
        raise MarketDataError(
            "Для работы с рыночными данными задайте TINKOFF_TOKEN в .env."
        )

    try:
        from tinkoff.invest import Client
    except ImportError as error:
        raise MarketDataError(
            "Клиент Tinkoff Invest API не установлен. "
            "Установите зафиксированные зависимости командой `uv sync --frozen`."
        ) from error

    return Client(
        settings.TINKOFF_TOKEN,
        interceptors=[
            RpcDeadlineInterceptor(settings.TINKOFF_RPC_TIMEOUT_SECONDS)
        ],
    )


def get_last_data(share):
    if settings.PRICE_DATA_MODE == "demo":
        observed_at = timezone.now()
        latest_demo_observation = (
            PriceObservation.objects.filter(
                share=share,
                source=PriceObservation.Source.DEMO,
            )
            .order_by("-observed_at")
            .first()
        )
        if (
            latest_demo_observation is not None
            and observed_at <= latest_demo_observation.observed_at
        ):
            observed_at = latest_demo_observation.observed_at + timedelta(
                microseconds=1
            )
        previous_price = (
            latest_demo_observation.price
            if latest_demo_observation is not None
            else Decimal("100.00")
        )
        value = previous_price + Decimal("0.25")
        record_price_observation(
            share,
            value,
            observed_at,
            PriceObservation.Source.DEMO,
        )
        logger.info("Generated explicitly enabled demo price for share %s", share.pk)
        return float(value), observed_at

    try:
        with get_client() as client:
            response = client.market_data.get_last_prices(figi=[share.figi])
    except MarketDataError:
        raise
    except Exception as error:
        raise _provider_error(
            error,
            f"получении цены акции {share.ticker}",
            f"Не удалось получить текущую цену для {share.ticker} из Tinkoff API.",
        ) from error

    if not response.last_prices:
        raise MarketDataError(
            f"Tinkoff API не вернул текущую цену для {share.ticker}."
        )

    price = response.last_prices[0]
    value = _quotation_to_decimal(price.price)
    record_price_observation(
        share,
        value,
        price.time,
        PriceObservation.Source.TINKOFF,
    )
    logger.info("Received last price for share %s", share.pk)
    return float(value), price.time
