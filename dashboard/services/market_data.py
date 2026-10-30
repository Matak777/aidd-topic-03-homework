"""Доступ к рыночным данным Tinkoff Invest API.

Обработка недоступности внешнего сервиса сделана явной:
- отсутствие TINKOFF_TOKEN даёт объяснимую ошибку, а не загадочный сбой gRPC;
- каждый реальный RPC ограничен настоящим gRPC-дедлайном (timeout= вызова):
  RPC прерывается клиентом при превышении, отмена серверного вызова и
  закрытие канала происходит в SDK-контексте клиента;
  run_with_timeout лишь страхует локальное ожидание результата;
- сетевые/gRPC-ошибки протоколируются и не обрывают работу прочих компонентов.
"""

import math
import threading

from tinkoff.invest import Client

from config.settings import TINKOFF_APP_NAME, TINKOFF_TOKEN, logger

# Буфер при ожидании потока сверх gRPC-дедлайна (конвертация ответов и пр.)
DEADLINE_MARGIN_S = 1.0

# Сервисы SDK, у которых подменяются стабы (стрим-сервисы не используются)
_SERVICE_NAMES = (
    "instruments",
    "market_data",
    "users",
    "orders",
    "stop_orders",
    "operations",
    "sandbox",
)


class TinkoffTokenMissing(RuntimeError):
    """TINKOFF_TOKEN не задан: интеграция с рынком недоступна."""


def validate_timeout(value, description: str) -> float:
    """Таймаут должен быть конечным положительным числом секунд."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Таймаут {description} должен быть числом секунд, получено: {value!r}"
        ) from None
    if math.isnan(timeout) or math.isinf(timeout) or timeout <= 0:
        raise ValueError(
            f"Таймаут {description} должен быть конечным положительным числом "
            f"секунд (ноль, отрицательные, NaN и бесконечность запрещены), "
            f"получено: {value!r}"
        )
    return timeout


class _DeadlineUnaryCallable:
    """Добавляет gRPC-дедлайн к unary-вызовам SDK (with_call/__call__)."""

    __slots__ = ("inner", "deadline_s")

    def __init__(self, inner, deadline_s: float):
        self.inner = inner
        self.deadline_s = deadline_s

    def with_call(self, request, **kwargs):
        kwargs.setdefault("timeout", self.deadline_s)
        return self.inner.with_call(request, **kwargs)

    def __call__(self, request, **kwargs):
        kwargs.setdefault("timeout", self.deadline_s)
        return self.inner(request, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _DeadlineStreamCallable:
    """Добавляет gRPC-дедлайн к стрим-вызовам SDK (стрим-итераторы)."""

    __slots__ = ("inner", "deadline_s")

    def __init__(self, inner, deadline_s: float):
        self.inner = inner
        self.deadline_s = deadline_s

    def __call__(self, request, **kwargs):
        kwargs.setdefault("timeout", self.deadline_s)
        return self.inner(request, **kwargs)

    def with_call(self, request, **kwargs):
        kwargs.setdefault("timeout", self.deadline_s)
        return self.inner.with_call(request, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class DeadlineStub:
    """Обёртка сгенерированного gRPC-стаба SDK: каждый RPC получает
    реальный клиентский дедлайн; при превышении RPC отменяется."""

    __slots__ = ("inner", "deadline_s")

    def __init__(self, inner, deadline_s: float):
        self.inner = inner
        self.deadline_s = deadline_s

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        # _MultiCallable в grpc — все unary/stream мультивызовы поддерживают
        # with_call; классы стримов начинаются на "Stream".
        if callable(attr) and hasattr(attr, "with_call"):
            is_stream = any(
                cls.__name__ == "StreamUnaryMultiCallable"
                for cls in type(attr).__mro__
            )
            wrapper = (
                _DeadlineStreamCallable
                if is_stream
                else _DeadlineUnaryCallable
            )
            return wrapper(attr, self.deadline_s)
        return attr


def apply_rpc_deadline(services, timeout_s: float) -> None:
    """Устанавливает gRPC-дедлайн на все методы синхронных сервисов SDK.

    services — экземпляр tinkoff.invest.Services (client.__enter__()).
    """
    timeout_s = validate_timeout(timeout_s, "RPC")
    for name in _SERVICE_NAMES:
        service = getattr(services, name, None)
        if service is not None and getattr(service, "stub", None) is not None:
            service.stub = DeadlineStub(service.stub, timeout_s)


def open_tinkoff_client():
    """Открывает gRPC-клиент Tinkoff; требует настроенный TINKOFF_TOKEN."""
    if not TINKOFF_TOKEN:
        raise TinkoffTokenMissing(
            "TINKOFF_TOKEN не задан: укажите токен Tinkoff Invest API в "
            "переменной окружения TINKOFF_TOKEN (см. .env.example), иначе "
            "рыночные данные недоступны."
        )
    return Client(TINKOFF_TOKEN, app_name=TINKOFF_APP_NAME)


def run_with_timeout(operation, timeout_s: float, description: str):
    """Выполняет операцию с ограничением времени ожидания.

    Основное ограничение — gRPC-дедлайн внутри RPC (см. apply_rpc_deadline);
    эта обёртка ограничивает суммарное локальное ожидание дедлайном + буфер.
    """
    timeout = validate_timeout(timeout_s, description)
    result, error = [], []

    def runner():
        try:
            result.append(operation())
        except (
            BaseException
        ) as e:  # noqa: BLE001 — протоколируем и пробрасываем
            error.append(e)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout + DEADLINE_MARGIN_S)
    if thread.is_alive():
        # Прекращение локального ожидания не отменяет сам RPC: его прерывает
        # клиентский gRPC-дедлайн, установленный в operation(). В ошибке это
        # честно оговорено; после ошибки поток завершит SDK-контекст клиента.
        raise TimeoutError(
            f"Операция {description} не завершилась за {timeout} с; ожидание "
            f"прервано (дальнейшую судьбу вызова смотри в журнале)."
        )
    if error:
        raise error[0]
    return result[0]


def money_to_float(quotation) -> float:
    """Преобразует Quotation (units + nano) в float без потери дробной части."""
    return float(quotation.units) + float(quotation.nano) / 1e9


def quotation_to_decimal(quotation) -> "decimal.Decimal":
    """Quotation (units + nano) → точный Decimal, без float-арифметики.

    Используется для накопления истории цен: запись в БД не должна терять
    нано-часть; float остаётся только для отрисовки графика."""
    import decimal

    return decimal.Decimal(quotation.units) + (
        decimal.Decimal(quotation.nano) / decimal.Decimal("1_000_000_000")
    )


def get_tinkoff_timeout() -> float:
    from config.settings import TINKOFF_TIMEOUT

    return TINKOFF_TIMEOUT


def get_last_prices(figi_list, timeout_s: float | None = None) -> dict:
    """Возвращает последнюю цену для списка figi одним gRPC-вызовом.

    Возвращает dict {figi: (price: Decimal — точно, time: datetime)}.
    """
    if not figi_list:
        return {}
    request_timeout = (
        timeout_s if timeout_s is not None else get_tinkoff_timeout()
    )
    validate_timeout(request_timeout, "get_last_prices")

    def request():
        with open_tinkoff_client() as client:
            apply_rpc_deadline(client, request_timeout)
            resp = client.market_data.get_last_prices(figi=list(figi_list))
            for item in resp.last_prices:
                logger.info(f"Get last data for share figi {item.figi}")
            return {
                item.figi: (quotation_to_decimal(item.price), item.time)
                for item in resp.last_prices
            }

    return run_with_timeout(request, request_timeout, "get_last_prices")
