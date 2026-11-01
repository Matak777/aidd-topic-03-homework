"""Стратегии виртуальных сделок (группа 4 FEATURES.md §4).

Условия и state machine переиспользуют мониторинг
(`watch_monitor._compatible_price`/`_matches`/`reset_baseline` — у
Strategy те же поля, что у WatchRule). Сделки — только виртуальные,
исключительно через общий сервис этапа 2 `execute_trade` (CAS, atomic,
идемпотентный ключ `strategy:{id}:obs:{id}`); брокерские ордера не
отправляются ни при каких настройках.

Политика (объявлена в FEATURES.md §4):

- создание — старая история не проигрывается (`reset_baseline`,
  on_change=False): первое новое подходящее наблюдение делает ровно
  одну попытку;
- пока условие выполняется — попыток нет; выход повторно взводит,
  вход — новая попытка; равенство порогу удовлетворяет;
- редактирование/включение — baseline по последнему ПРИГОДНОМУ
  совместимому небудущему наблюдению без исполнения (невалидная
  последняя точка не сбрасывает baseline);
- окно наблюдений — устойчивый порядок (время, id); запоздавшая точка
  не откатывает состояние; повторная обработка не дублирует попытки/
  сделки (уникальность (стратегия, наблюдение));
- ТЕКУЩАЯ СИМУЛЯЦИЯ: время исполнения = now процесса, не время
  котировки (никаких сделок задним числом); хронология портфеля и
  деньги/остаток проверяет execute_trade — недостаток записывается
  журналом отказа без изменения портфеля и расходует попытку;
- подлинный сбой записи не считается дублем/успехом: IntegrityError
  признаётся дублем только после подтверждения существующей записи
  (как в ревизии мониторинга), иначе вся попытка (журнал, состояние,
  курсор) откатывается и обрабатывается повторно; ошибка одной
  стратегии не останавливает остальные.
"""

import logging
from decimal import DecimalException

from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.models import (
    Portfolio,
    PriceObservation,
    Strategy,
    StrategyAttempt,
)
from dashboard.services.portfolio_accounting import (
    TradeValidationError,
    execute_trade,
    validate_metric,
)
from dashboard.services.watch_monitor import (
    _compatible_price,
    _matches,
    reset_baseline,
)

logger = logging.getLogger(__name__)


def idempotency_key(strategy, obs) -> str:
    """Ключ идемпотентности сделки: связывает стратегию и наблюдение."""
    return f"strategy:{strategy.id}:obs:{obs.id}"


def _attempt_snapshot(strategy, obs, price) -> dict:
    """Снимок попытки: изменение/удаление стратегии не перепишет её."""
    share = strategy.share
    portfolio = strategy.portfolio
    return {
        "user": strategy.user,
        "strategy": strategy,
        "observation": obs,
        "portfolio": portfolio,
        "strategy_name": strategy.name,
        "figi": share.figi,
        "ticker": share.ticker,
        "share_name": share.name,
        "share_currency": share.currency,
        "portfolio_name": portfolio.name,
        "portfolio_currency": portfolio.base_currency,
        "direction": strategy.direction,
        "threshold": strategy.threshold,
        "source": strategy.source,
        "currency": strategy.currency,
        "side": strategy.side,
        "quantity": strategy.quantity,
        "commission": strategy.commission,
        "observed_price": str(price),
        "quote_time": obs.quote_time,
    }


def _execution_refusal(strategy, price):
    """Validate execution independently of the HTML form.

    A persisted strategy can outlive changes to related objects, and future
    callers need the same owner/source/currency checks as the current UI.
    Expected configuration or numeric errors consume an attempt with a
    reason; they must not pin the processing cursor on every collector run.
    """
    portfolio = strategy.portfolio
    if portfolio.user_id != strategy.user_id:
        return "Владелец портфеля не совпадает с владельцем стратегии."
    if (
        strategy.source
        not in (
            PriceObservation.SOURCE_DEMO,
            PriceObservation.SOURCE_TINKOFF,
        )
        or strategy.source != portfolio.valuation_source
    ):
        return "Источник стратегии не совпадает с источником оценки портфеля."
    currencies = {
        (value or "").strip().upper()
        for value in (
            strategy.currency,
            strategy.share.currency,
            portfolio.base_currency,
        )
    }
    if len(currencies) != 1 or "" in currencies:
        return "Валюты стратегии, инструмента и портфеля не совпадают."
    if strategy.direction not in dict(Strategy.DIRECTION_CHOICES):
        return "Неизвестное направление условия стратегии."
    if strategy.side not in dict(Strategy.SIDE_CHOICES):
        return "Неизвестная сторона виртуальной сделки."
    try:
        validate_metric(strategy.threshold, positive=True, label="порог")
        validate_metric(strategy.quantity, positive=True, label="количество")
        validate_metric(strategy.commission, positive=False, label="комиссия")
        validate_metric(price, positive=True, label="цена наблюдения")
    except (ValueError, DecimalException) as exc:
        return str(exc)
    return None


@transaction.atomic
def process_strategy(strategy, *, now=None) -> int:
    """Атомарная обработка окна новых наблюдений одной стратегии.

    Возвращает число зафиксированных попыток (исполненных и
    отказанных). Строка стратегии блокируется (select_for_update);
    SQLite сериализует запись движком (ограничение см. FEATURES.md §1).
    """
    locked = Strategy.objects.select_for_update().get(id=strategy.id)
    if not locked.enabled:
        return 0
    if now is None:
        now = timezone.now()
    window = PriceObservation.objects.filter(
        share=locked.share, id__gt=locked.cursor_id
    ).order_by("id")
    scanned_max = locked.cursor_id
    candidates = []
    for obs in window:
        scanned_max = max(scanned_max, obs.id)
        price = _compatible_price(locked, obs)
        if price is None:
            # чужой источник/валюта или некорректная цена: наблюдение
            # неизменяемо — решение окончательно, окно двигается
            continue
        if obs.quote_time is None or obs.quote_time > now:
            # будущая точка состояние не меняет (окончательно)
            continue
        candidates.append((obs.quote_time, obs.id, obs, price))

    candidates.sort(key=lambda item: (item[0], item[1]))
    head = locked.head_quote_time
    state = locked.state
    attempts = []
    for quote_time, _obs_id, obs, price in candidates:
        # запоздавшая точка: не откатывает состояние, не ретро-попытка
        if head is not None and obs.quote_time < head:
            continue
        try:
            satisfied = _matches(locked, price)
        except (ValueError, DecimalException):
            # Corrupt condition parameters cannot execute a trade. Record
            # one explicit refusal; editing the strategy resets its baseline.
            if state != Strategy.STATE_TRIGGERED:
                attempts.append((obs, price))
                state = Strategy.STATE_TRIGGERED
            head = obs.quote_time
            continue
        if state == Strategy.STATE_TRIGGERED:
            if satisfied:
                pass  # пока условие выполнено — попыток нет
            else:
                state = Strategy.STATE_ARMED  # выход повторно взводит
        elif satisfied:
            attempts.append((obs, price))
            state = Strategy.STATE_TRIGGERED
        head = obs.quote_time

    locked.cursor_id = scanned_max
    locked.head_quote_time = head
    locked.state = state
    locked.save()

    recorded = 0
    for obs, price in attempts:
        if _run_attempt(locked, obs, price, now=now):
            recorded += 1
    return recorded


def _run_attempt(strategy, obs, price, *, now) -> bool:
    """Одна попытка: журнал + сделка через execute_trade.

    Возвращает False, если попытка — подтверждённый дубль (повторная
    обработка той же пары `strategy`/`observation`); True — записана
    новая попытка (исполненная или отказанная). Иной сбой записи
    пробрасывается: вся транзакция process_strategy откатывается —
    состояние/курсор/журнал повторяемы.
    """
    # Serialise strategies sharing a portfolio where row locking is
    # supported; SQLite still relies on its transaction/write lock and CAS.
    strategy.portfolio = Portfolio.objects.select_for_update().get(
        id=strategy.portfolio_id
    )
    refusal = _execution_refusal(strategy, price)
    # Journal, trade, cash and state all remain in the outer transaction.
    try:
        with transaction.atomic():
            attempt = StrategyAttempt.objects.create(
                **_attempt_snapshot(strategy, obs, price),
                decision=StrategyAttempt.DECISION_REJECTED,
                reason=(refusal or "")[:255],
            )
    except IntegrityError as e:
        confirmed = StrategyAttempt.objects.filter(
            strategy=strategy, observation=obs, user=strategy.user
        ).exists()
        if confirmed:
            # дубль создал параллельный обработчик: идемпотентно
            return False
        logger.warning(
            f"strategy_runner: attempt persist failed for strategy "
            f"{strategy.id} observation {obs.id}: {e}"
        )
        raise
    if refusal:
        return True
    try:
        trade = execute_trade(
            strategy.portfolio,
            side=strategy.side,
            share=strategy.share,
            quantity=strategy.quantity,
            price=str(price),
            commission=strategy.commission,
            # `now` is the observation-selection cutoff, possibly captured
            # before RPCs or an entire backlog. Execution uses the current
            # clock on each attempt, including successive portfolio writers.
            executed_at=timezone.now(),
            note=f"strategy:{strategy.id}",
            idempotency_key=idempotency_key(strategy, obs),
        )
    except (ValueError, DecimalException) as e:
        if isinstance(e, TradeValidationError) and e.__cause__ is not None:
            # execute_trade wraps failures of Trade.objects.create. Those
            # are persistence errors, not business refusals: preserve the
            # whole-window rollback and allow a later retry.
            raise
        # отказ записан; портфель не изменён; попытка «израсходована»
        attempt.decision = StrategyAttempt.DECISION_REJECTED
        attempt.reason = str(e)[:255]
        attempt.save()
        return True
    attempt.trade = trade
    attempt.decision = StrategyAttempt.DECISION_EXECUTED
    attempt.reason = ""
    attempt.save()
    return True


def process_share(share, *, now=None) -> int:
    """Все включённые стратегии инструмента.

    Подключено к общему пути записи наблюдений (`apply_prices`);
    ошибка одной стратегии журнализуется и не прерывает остальные
    стратегии, мониторинг и цикл записи.
    """
    total = 0
    strategies = (
        Strategy.objects.filter(share=share, enabled=True)
        .order_by("id")
        .select_related("portfolio")
    )
    for strategy in strategies:
        try:
            total += process_strategy(strategy, now=now)
        except Exception as e:
            logger.warning(
                f"strategy_runner: strategy {strategy.id} failed: {e}"
            )
    return total
