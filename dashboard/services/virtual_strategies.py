import logging
from datetime import timedelta
from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from dashboard.models import (
    PortfolioTransaction,
    PriceObservation,
    StrategyEvaluation,
    TradingStrategy,
)
from dashboard.services.portfolios import execute_portfolio_transaction

logger = logging.getLogger("main")
_STRATEGY_TRANSACTION_NAMESPACE = UUID("e236e3f1-04c7-45a3-a6e4-77239c398b25")


def is_satisfied(strategy, price):
    if strategy.direction == TradingStrategy.Direction.AT_LEAST:
        return price >= strategy.threshold
    return price <= strategy.threshold


def _latest_series_observation(strategy):
    if strategy.share_id is None:
        return None
    baseline_at = timezone.now()
    observations = PriceObservation.objects.filter(
        share_id=strategy.share_id,
        source=strategy.source,
        currency__iexact=strategy.currency,
        observed_at__lte=baseline_at,
        observed_at__gte=baseline_at
        - timedelta(seconds=settings.STRATEGY_MAX_OBSERVATION_AGE_SECONDS),
        price__gt=0,
    )
    if strategy.baseline_at is not None:
        observations = observations.filter(received_at__lte=strategy.baseline_at)
    return observations.order_by("-observed_at", "-pk").first()


def initialize_strategy(strategy, *, new_strategy=False):
    baseline_at = timezone.now()
    strategy.baseline_at = baseline_at
    observation = _latest_series_observation(strategy)
    if observation is None:
        strategy.cursor_observed_at = None
        strategy.cursor_observation_id = None
        strategy.armed = True
    else:
        strategy.cursor_observed_at = observation.observed_at
        strategy.cursor_observation_id = observation.pk
        strategy.armed = (
            True if new_strategy else not is_satisfied(strategy, observation.price)
        )
    strategy.state_initialized = True
    strategy.save(
        update_fields=(
            "cursor_observed_at",
            "cursor_observation_id",
            "armed",
            "state_initialized",
            "baseline_at",
            "updated_at",
        )
    )


def _after_cursor(strategy):
    observations = PriceObservation.objects.filter(
        share_id=strategy.share_id,
        source=strategy.source,
    )
    if strategy.baseline_at is not None:
        observations = observations.filter(received_at__gt=strategy.baseline_at)
    return observations.order_by("observed_at", "pk")


def _make_evaluation(strategy, observation, *, decision, reason="", transaction_record=None):
    return StrategyEvaluation.objects.create(
        owner_id=strategy.owner_id,
        portfolio_id=strategy.portfolio_id,
        strategy=strategy,
        strategy_name=strategy.name,
        share_id=strategy.share_id,
        share_ticker=strategy.share_ticker,
        share_name=strategy.share_name,
        observation=observation,
        observation_key=observation.pk,
        observed_price=observation.price,
        observed_currency=observation.currency,
        observed_source=observation.source,
        observed_at=observation.observed_at,
        direction=strategy.direction,
        threshold=strategy.threshold,
        action=strategy.action,
        quantity=strategy.quantity,
        commission=strategy.commission,
        decision=decision,
        reason=reason,
        transaction=transaction_record,
    )


def _observation_can_change_state(strategy, observation, *, now):
    max_age = timedelta(seconds=settings.STRATEGY_MAX_OBSERVATION_AGE_SECONDS)
    cursor = (strategy.cursor_observed_at, strategy.cursor_observation_id or 0)
    event = (observation.observed_at, observation.pk)
    return (
        observation.share_id == strategy.share_id
        and observation.source == strategy.source
        and observation.currency.casefold() == strategy.currency.casefold()
        and observation.observed_at <= now
        and now - observation.observed_at <= max_age
        and observation.price.is_finite()
        and observation.price > 0
        and (
            strategy.cursor_observed_at is None
            or event > cursor
        )
    )


@transaction.atomic
def _record_unexpected_failure(strategy_id, observation_id):
    strategy = TradingStrategy.objects.filter(pk=strategy_id).first()
    observation = PriceObservation.objects.filter(pk=observation_id).first()
    if strategy is None or observation is None:
        return
    if StrategyEvaluation.objects.filter(
        strategy=strategy,
        observation_key=observation.pk,
    ).exists():
        return
    _make_evaluation(
        strategy,
        observation,
        decision=StrategyEvaluation.Decision.REJECTED,
        reason="Внутренняя ошибка обработки; баланс и позиция не были изменены.",
    )
    if (
        _observation_can_change_state(
            strategy,
            observation,
            now=timezone.now(),
        )
        and is_satisfied(strategy, observation.price)
    ):
        strategy.armed = False
        strategy.cursor_observed_at = observation.observed_at
        strategy.cursor_observation_id = observation.pk
        strategy.save(
            update_fields=(
                "armed",
                "cursor_observed_at",
                "cursor_observation_id",
                "updated_at",
            )
        )


@transaction.atomic
def _process_strategy_observation(strategy_id, observation_id):
    strategy = (
        TradingStrategy.objects.select_for_update()
        .filter(pk=strategy_id, enabled=True)
        .first()
    )
    if strategy is None:
        return False
    observation = PriceObservation.objects.filter(pk=observation_id).first()
    if observation is None:
        return False
    if StrategyEvaluation.objects.filter(
        strategy=strategy,
        observation_key=observation.pk,
    ).exists():
        return False

    now = timezone.now()
    if observation.share_id != strategy.share_id:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Инструмент наблюдения не совпадает с инструментом стратегии.",
        )
        return True
    if observation.source != strategy.source:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Источник наблюдения не совпадает с источником стратегии.",
        )
        return True
    if observation.currency.casefold() != strategy.currency.casefold():
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Валюта наблюдения не совпадает с валютой стратегии.",
        )
        return True
    if observation.observed_at > now:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Время котировки находится в будущем.",
        )
        return True
    if now - observation.observed_at > timedelta(
        seconds=settings.STRATEGY_MAX_OBSERVATION_AGE_SECONDS
    ):
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason=(
                "Котировка устарела: допустимый возраст "
                f"{settings.STRATEGY_MAX_OBSERVATION_AGE_SECONDS} сек."
            ),
        )
        return True
    if not observation.price.is_finite() or observation.price <= 0:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Цена котировки должна быть конечным положительным числом.",
        )
        return True
    cursor = (strategy.cursor_observed_at, strategy.cursor_observation_id or 0)
    event = (observation.observed_at, observation.pk)
    if strategy.cursor_observed_at is not None and event <= cursor:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Запоздавшее наблюдение не меняет уже обработанное состояние.",
        )
        return True

    if strategy.share_id is None:
        _make_evaluation(
            strategy,
            observation,
            decision=StrategyEvaluation.Decision.SKIPPED,
            reason="Инструмент стратегии удалён.",
        )
        return True

    satisfied = is_satisfied(strategy, observation.price)
    transaction_record = None
    if not satisfied:
        decision = (
            StrategyEvaluation.Decision.REARMED
            if not strategy.armed
            else StrategyEvaluation.Decision.NO_TRIGGER
        )
        if not strategy.armed:
            strategy.armed = True
        reason = "Цена не удовлетворяет условию."
    elif not strategy.armed:
        decision = StrategyEvaluation.Decision.NO_TRIGGER
        reason = "Срабатывание уже израсходовано; ожидается выход из условия."
    else:
        strategy.armed = False
        idempotency_key = uuid5(
            _STRATEGY_TRANSACTION_NAMESPACE,
            f"{strategy.pk}:{observation.pk}",
        )
        try:
            transaction_record, _created = execute_portfolio_transaction(
                portfolio_id=strategy.portfolio_id,
                share_id=strategy.share_id,
                side=strategy.action,
                quantity=strategy.quantity,
                execution_price=observation.price,
                commission=strategy.commission,
                currency=observation.currency,
                executed_at=now,
                idempotency_key=idempotency_key,
                execution_type=PortfolioTransaction.ExecutionType.STRATEGY,
                strategy=strategy,
                strategy_name=strategy.name,
                execution_observation=observation,
            )
        except ValidationError as error:
            decision = StrategyEvaluation.Decision.REJECTED
            reason = "; ".join(error.messages)
        else:
            decision = StrategyEvaluation.Decision.EXECUTED
            reason = (
                "Виртуальная сделка исполнена по сохранённой котировке; "
                "внешний ордер не отправлялся."
            )

    strategy.cursor_observed_at = observation.observed_at
    strategy.cursor_observation_id = observation.pk
    strategy.save(
        update_fields=(
            "armed",
            "cursor_observed_at",
            "cursor_observation_id",
            "updated_at",
        )
    )
    _make_evaluation(
        strategy,
        observation,
        decision=decision,
        reason=reason,
        transaction_record=transaction_record,
    )
    return True


def process_virtual_strategies(*, share_id=None, observation_id=None):
    strategies = TradingStrategy.objects.filter(
        enabled=True,
        owner_id=F("portfolio__owner_id"),
    )
    if share_id is not None:
        strategies = strategies.filter(share_id=share_id)
    strategy_ids = list(strategies.order_by("portfolio_id", "pk").values_list("pk", flat=True))

    processed = 0
    for strategy_id in strategy_ids:
        strategy = TradingStrategy.objects.filter(pk=strategy_id, enabled=True).first()
        if strategy is None or strategy.share_id is None:
            continue
        if not strategy.state_initialized:
            with transaction.atomic():
                locked = TradingStrategy.objects.select_for_update().get(pk=strategy_id)
                if not locked.state_initialized:
                    initialize_strategy(locked, new_strategy=True)
            strategy.refresh_from_db()

        if observation_id is not None:
            observations = PriceObservation.objects.filter(pk=observation_id)
        else:
            observations = _after_cursor(strategy).exclude(
                strategy_evaluations__strategy_id=strategy_id
            )
        if strategy.baseline_at is not None:
            observations = observations.filter(received_at__gt=strategy.baseline_at)
        observation_ids = list(
            observations.order_by("observed_at", "pk").values_list("pk", flat=True)
        )
        for current_observation_id in observation_ids:
            try:
                processed += int(
                    _process_strategy_observation(
                        strategy_id,
                        current_observation_id,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to evaluate virtual strategy %s on observation %s",
                    strategy_id,
                    current_observation_id,
                )
                try:
                    _record_unexpected_failure(
                        strategy_id,
                        current_observation_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record strategy failure for %s on observation %s",
                        strategy_id,
                        current_observation_id,
                    )
    return processed
