from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from dashboard.models import (
    PortfolioPosition,
    PortfolioTransaction,
    PriceObservation,
    Share,
    VirtualPortfolio,
)

AMOUNT_QUANTUM = Decimal("0.000000001")


def _high_precision(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 80
            return function(*args, **kwargs)

    return wrapped


def quantize_amount(value):
    with localcontext() as context:
        context.prec = 80
        try:
            amount = Decimal(str(value)).quantize(
                AMOUNT_QUANTUM,
                rounding=ROUND_HALF_EVEN,
            )
        except InvalidOperation as error:
            raise ValidationError(
                "Сумма выходит за поддерживаемую точность портфеля."
            ) from error
    if abs(amount) >= Decimal("1e21"):
        raise ValidationError(
            "Сумма превышает максимум DecimalField (21 целая цифра и 9 знаков)."
        )
    return amount


def validate_storable_amount(value):
    amount = quantize_amount(value)
    if connection.vendor == "sqlite":
        digits = amount.copy_abs().as_tuple().digits
        significant_digits = len(digits)
        while significant_digits > 1 and digits[significant_digits - 1] == 0:
            significant_digits -= 1
        if significant_digits > 15:
            raise ValidationError(
                "SQLite не может точно сохранить эту сумму: используйте не "
                "более 15 значащих цифр (до 9 знаков после запятой)."
            )
    return amount


def validate_input_amount(value):
    try:
        raw_amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError("Укажите корректную сумму.") from error
    if not raw_amount.is_finite():
        raise ValidationError("Сумма должна быть конечным числом.")
    amount = validate_storable_amount(raw_amount)
    if raw_amount != amount:
        raise ValidationError(
            "Поддерживается не более 9 знаков после запятой; значение не "
            "будет округлено при вводе."
        )
    return amount


def _normalized_currency(value):
    return value.strip().upper()


def _same_transaction(transaction_record, *, portfolio, share, side, quantity,
                      execution_price, commission, currency, executed_at):
    return (
        transaction_record.portfolio_id == portfolio.pk
        and transaction_record.share_id == share.pk
        and transaction_record.side == side
        and transaction_record.quantity == quantity
        and transaction_record.execution_price == execution_price
        and transaction_record.commission == commission
        and transaction_record.currency.casefold() == currency.casefold()
        and transaction_record.executed_at == executed_at
    )


@_high_precision
@transaction.atomic
def execute_portfolio_transaction(
    *,
    portfolio_id,
    share_id,
    side,
    quantity,
    execution_price,
    commission,
    currency,
    executed_at,
    idempotency_key,
    execution_type=PortfolioTransaction.ExecutionType.MANUAL,
    strategy=None,
    strategy_name="",
    execution_observation=None,
):
    portfolio = (
        VirtualPortfolio.objects.select_for_update()
        .filter(pk=portfolio_id)
        .first()
    )
    if portfolio is None:
        raise ValidationError("Портфель больше не существует.")

    share = Share.objects.filter(pk=share_id).first()
    if share is None:
        raise ValidationError("Выбранный инструмент больше не существует.")

    quantity = validate_input_amount(quantity)
    execution_price = validate_input_amount(execution_price)
    commission = validate_input_amount(commission)
    currency = _normalized_currency(currency)
    if side not in PortfolioTransaction.Side.values:
        raise ValidationError("Неизвестная сторона виртуальной операции.")
    if quantity <= 0 or execution_price <= 0 or commission < 0:
        raise ValidationError("Проверьте количество, цену и комиссию операции.")
    if not timezone.is_aware(executed_at):
        raise ValidationError("Время операции должно содержать часовой пояс.")
    if executed_at > timezone.now():
        raise ValidationError("Время операции не может быть в будущем.")
    if currency.casefold() != portfolio.currency.casefold():
        raise ValidationError(
            f"Валюта операции должна совпадать с валютой портфеля "
            f"({portfolio.currency})."
        )
    if share.currency.casefold() != portfolio.currency.casefold():
        raise ValidationError(
            f"Валюта инструмента {share.currency} несовместима с портфелем "
            f"{portfolio.currency}; конвертация не выполняется."
        )
    if execution_observation is not None:
        if execution_observation.share_id != share.pk:
            raise ValidationError(
                "Наблюдение котировки относится к другому инструменту."
            )
        if (
            execution_observation.price != execution_price
            or execution_observation.currency.casefold() != currency.casefold()
        ):
            raise ValidationError(
                "Цена или валюта операции не совпадает со снимком котировки."
            )
        if execution_observation.observed_at > executed_at:
            raise ValidationError(
                "Нельзя исполнить виртуальную операцию по будущей котировке."
            )
    if execution_type == PortfolioTransaction.ExecutionType.STRATEGY:
        if strategy is None or execution_observation is None:
            raise ValidationError(
                "Для операции стратегии обязательны стратегия и котировка."
            )
        if (
            strategy.share_id != share.pk
            or strategy.portfolio_id != portfolio.pk
            or strategy.owner_id != portfolio.owner_id
        ):
            raise ValidationError(
                "Стратегия, портфель и инструмент операции не согласованы."
            )
        if (
            strategy.action != side
            or strategy.quantity != quantity
            or strategy.commission != commission
            or strategy.source != execution_observation.source
            or strategy.currency.casefold()
            != execution_observation.currency.casefold()
        ):
            raise ValidationError(
                "Параметры операции не совпадают со снимком стратегии и котировки."
            )

    existing = PortfolioTransaction.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if _same_transaction(
            existing,
            portfolio=portfolio,
            share=share,
            side=side,
            quantity=quantity,
            execution_price=execution_price,
            commission=commission,
            currency=currency,
            executed_at=executed_at,
        ):
            return existing, False
        raise ValidationError(
            "Ключ повтора уже использован для другой операции."
        )

    last_transaction = (
        PortfolioTransaction.objects.filter(portfolio=portfolio)
        .order_by("-executed_at", "-pk")
        .first()
    )
    if last_transaction is not None and executed_at < last_transaction.executed_at:
        raise ValidationError(
            "Дата операции не может быть раньше последней операции портфеля."
        )

    position = (
        PortfolioPosition.objects.select_for_update()
        .filter(portfolio=portfolio, share=share)
        .first()
    )
    if position is None:
        position = PortfolioPosition(
            portfolio=portfolio,
            share=share,
            share_ticker=share.ticker,
            share_name=share.name,
        )

    gross_amount = validate_storable_amount(quantity * execution_price)
    if side == PortfolioTransaction.Side.BUY:
        total_cost = validate_storable_amount(gross_amount + commission)
        if total_cost > portfolio.cash_balance:
            raise ValidationError("Недостаточно виртуальных денег для покупки.")
        new_quantity = validate_storable_amount(position.quantity + quantity)
        new_basis = validate_storable_amount(position.cost_basis + total_cost)
        new_average_cost = validate_storable_amount(new_basis / new_quantity)
        new_cash_balance = validate_storable_amount(
            portfolio.cash_balance - total_cost
        )
        new_realized_pnl = position.realized_pnl
    else:
        if quantity > position.quantity:
            raise ValidationError("Нельзя продать больше открытого количества.")
        net_proceeds = validate_storable_amount(gross_amount - commission)
        if net_proceeds < 0 and portfolio.cash_balance + net_proceeds < 0:
            raise ValidationError(
                "Комиссия превышает выручку, а виртуального баланса недостаточно."
            )
        old_quantity = position.quantity
        new_quantity = validate_storable_amount(old_quantity - quantity)
        if new_quantity == 0:
            disposed_basis = position.cost_basis
            new_basis = Decimal("0")
            new_average_cost = Decimal("0")
        else:
            disposed_basis = validate_storable_amount(
                position.cost_basis * quantity / old_quantity
            )
            new_basis = validate_storable_amount(
                position.cost_basis - disposed_basis
            )
            new_average_cost = validate_storable_amount(new_basis / new_quantity)
        realized = validate_storable_amount(net_proceeds - disposed_basis)
        new_realized_pnl = validate_storable_amount(
            position.realized_pnl + realized
        )
        new_cash_balance = validate_storable_amount(
            portfolio.cash_balance + net_proceeds
        )

    try:
        position.quantity = new_quantity
        position.cost_basis = new_basis
        position.average_cost = new_average_cost
        position.realized_pnl = new_realized_pnl
        position.save()
        portfolio.cash_balance = new_cash_balance
        portfolio.save(update_fields=("cash_balance",))
        transaction_record = PortfolioTransaction.objects.create(
            portfolio=portfolio,
            share=share,
            share_ticker=share.ticker,
            share_name=share.name,
            side=side,
            quantity=quantity,
            execution_price=execution_price,
            commission=commission,
            currency=currency,
            executed_at=executed_at,
            idempotency_key=idempotency_key,
            execution_type=execution_type,
            strategy=strategy,
            strategy_name=strategy_name,
            execution_observation=execution_observation,
            quote_source=(
                execution_observation.source if execution_observation else ""
            ),
            quote_currency=(
                execution_observation.currency if execution_observation else ""
            ),
            quote_observed_at=(
                execution_observation.observed_at if execution_observation else None
            ),
        )
    except IntegrityError as error:
        raise ValidationError(
            "Операция конфликтует с уже сохранённой записью; обновите страницу "
            "и проверьте журнал."
        ) from error
    return transaction_record, True


@dataclass(frozen=True)
class PositionValuation:
    position: PortfolioPosition
    market_price: Decimal | None
    market_observed_at: datetime | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    price_source: str
    price_source_label: str


@dataclass(frozen=True)
class PortfolioValuation:
    portfolio: VirtualPortfolio
    valued_at: datetime
    positions: list[PositionValuation]
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    total_value: Decimal | None
    total_result: Decimal | None
    return_percentage: Decimal | None
    is_complete: bool


@_high_precision
def value_portfolio(portfolio, *, valued_at=None):
    valued_at = valued_at or timezone.now()
    positions = []
    realized_total = Decimal("0")
    unrealized_total = Decimal("0")
    market_total = Decimal("0")
    is_complete = True

    for position in portfolio.positions.select_related("share").all():
        realized_total += position.realized_pnl
        if position.quantity == 0:
            positions.append(
                PositionValuation(
                    position=position,
                    market_price=None,
                    market_observed_at=None,
                    market_value=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                    price_source=portfolio.valuation_source,
                    price_source_label=PriceObservation.Source(
                        portfolio.valuation_source
                    ).label,
                )
            )
            continue

        observation = None
        if position.share_id is not None:
            observation = (
                PriceObservation.objects.filter(
                    share_id=position.share_id,
                    source=portfolio.valuation_source,
                    currency__iexact=portfolio.currency,
                    observed_at__lte=valued_at,
                )
                .order_by("-observed_at", "-pk")
                .first()
            )
        if observation is None:
            is_complete = False
            positions.append(
                PositionValuation(
                    position=position,
                    market_price=None,
                    market_observed_at=None,
                    market_value=None,
                    unrealized_pnl=None,
                    price_source=portfolio.valuation_source,
                    price_source_label=PriceObservation.Source(
                        portfolio.valuation_source
                    ).label,
                )
            )
            continue

        market_value = quantize_amount(position.quantity * observation.price)
        unrealized = quantize_amount(market_value - position.cost_basis)
        market_total += market_value
        unrealized_total += unrealized
        positions.append(
            PositionValuation(
                position=position,
                market_price=observation.price,
                market_observed_at=observation.observed_at,
                market_value=market_value,
                unrealized_pnl=unrealized,
                price_source=observation.source,
                price_source_label=observation.get_source_display(),
            )
        )

    if is_complete:
        total_value = quantize_amount(portfolio.cash_balance + market_total)
        total_result = quantize_amount(total_value - portfolio.starting_cash)
        unrealized_pnl = quantize_amount(unrealized_total)
        return_percentage = (
            (total_result / portfolio.starting_cash * Decimal("100")).quantize(
                Decimal("0.000001"),
                rounding=ROUND_HALF_EVEN,
            )
        )
    else:
        total_value = None
        total_result = None
        unrealized_pnl = None
        return_percentage = None

    return PortfolioValuation(
        portfolio=portfolio,
        valued_at=valued_at,
        positions=positions,
        realized_pnl=quantize_amount(realized_total),
        unrealized_pnl=unrealized_pnl,
        total_value=total_value,
        total_result=total_result,
        return_percentage=return_percentage,
        is_complete=is_complete,
    )
