import logging
import math
from collections import namedtuple

import grpc
from django.conf import settings

logger = logging.getLogger("main")


class MarketDataError(RuntimeError):
    """Raised when market data cannot be obtained from the provider."""


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
    value = price.price.units + price.price.nano / 1_000_000_000
    logger.info("Received last price for share %s", share.pk)
    return value, price.time
