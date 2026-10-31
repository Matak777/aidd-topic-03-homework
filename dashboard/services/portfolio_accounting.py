"""Учёт виртуальных портфелей (этап 2, FEATURES.md §2).

Совместный сервис: представления HTML и будущие стратегии (этап 4)
вызывают одни и те же функции — валидация величин, позиции и P/L,
подбор котировки, атомарное исполнение операции.

Политика точности (F2.5, зафиксирована по факту ревизии):

- все деньги/количества — Decimal; в журнал и cash записываются
  канонические тексты Decimal — без ведущих нулей, без экспоненты
  и без НЕЗНАЧАЩИХ хвостовых нулей ('1.000000000000000000' → '1');
  канонизация — точные строковые операции над format_exact и НЕ
  зависит от текущего Decimal-контекста (нельзя заменять её округляющим
  normalize() в контексте 28 — он изменил бы большие точные значения).
  Математическое значение ввода не меняется; CAS-сравнение текстов
  остаётся согласован при уже сохранённых неканонических, но
  корректных записях;
- допускаемые значения: до 30 цифр в целой части и до 18 ЗНАЧАЩИХ —
  в дробной (хвостовые нули не увеличивают требуемую точность).
  Значения ВНЕ этого диапазона отклоняются ДО любого изменения БД
  (ни cash, ни журнал не меняются) — и в формах, и в сервисе;
- внутренние вычисления производных величин (позиции, P/L, итоги)
  выполняются в широком вычислительном контексте CALC_PREC (120 цифр)
  с ЯВНО заданной политикой: prec, правило округления ROUND_HALF_EVEN
  и отключённые traps Inexact/Overflow задаются в контексте и НЕ
  наследуются от вызывающего (иначе средняя себестоимость 301/3
  падала бы decimal.Inexact внутри точного контекста исполнения);
  умножения/сложения допускаемых входов точны, деление (средняя
  себестоимость, доходность) округляется в этом контексте — для
  нетривиальных дробей точность принципиально недостижима, и это
  объявленное, одинаковое во всех путях правило;
- денежные суммы исполнения (дельта cash) вычисляются в отдельном
  контексте с trap'ами Inexact/Overflow: потеря точности денег —
  отказ, а не молчаливое округление (контроль потери сохраняется);
- отображение агрегатов округляется явно и только для показа
  (quantize до 4 знаков) — журнал и cash остаются источником истины;
- NaN/бесконечность/переполнение отклоняются.
"""

import hashlib
import json
from contextlib import contextmanager
from datetime import timezone as datetime_tz
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    getcontext,
    localcontext,
)

from django.db import transaction
from django.utils import timezone

from dashboard.models import Portfolio, Trade, format_exact

# Политика округления для отображения агрегатов: 4 знака после запятой.
DISPLAY_EXPONENT = Decimal("0.0001")
# Ограничения величин (защита от переполнения и абсурда):
MAX_INT_DIGITS = 30
MAX_FRACTION_DIGITS = 18
MAX_VALUE = Decimal("9" * MAX_INT_DIGITS + "." + "9" * MAX_FRACTION_DIGITS)

# Широкий вычислительный контекст: допускаемые входы (≤ 48 значащих
# цифр каждый) при умножении/сложении дают не более ~97-98 значащих
# цифр, поэтому 120 достаточно для ТОЧНОГО вычисления без округления.
CALC_PREC = 120


def _set_calc_policy(ctx):
    """Явная политика вычислительного контекста (без наследования).

    Задаются prec, правило округления и состояние trap'ов: контекст не
    должен случайно унаследовать ни точность, ни trap'ы Inexact/Overflow
    вызывающего — иначе средняя себестоимость 301/3 падала бы
    decimal.Inexact внутри точного контекста исполнения.
    """
    ctx.prec = CALC_PREC
    ctx.rounding = ROUND_HALF_EVEN
    ctx.traps[Inexact] = False
    ctx.traps[Overflow] = False
    return ctx


@contextmanager
def derivations_context():
    """Контекст производных величин (позиции/P/L/итоги).

    Умножения и суммы допускаемых входов точны; деление (средняя
    себестоимость, доходность) округляется с ЯВНО заданным правилом
    ROUND_HALF_EVEN в CALC_PREC — для нетривиальных дробей (301/3)
    точность принципиально недостижима. Trap'ы отключены явственно,
    а не по отсутствию наследования от вызывающего.
    """
    with localcontext() as ctx:
        _set_calc_policy(ctx)
        yield


@contextmanager
def exact_calc_context():
    """Контекст денежных сумм исполнения: Inexact/Overflow — ошибки.

    Дельта cash и записываемые величины вычисляются здесь; если
    вычисление не может быть точным — операция отклоняется вместо
    молчаливого округления (контроль потери денег сохраняется).
    """
    with localcontext() as ctx:
        ctx.prec = CALC_PREC
        ctx.rounding = ROUND_HALF_EVEN
        ctx.traps[Inexact] = True
        ctx.traps[Overflow] = True
        yield


def display_round(value):
    """Явное округление агрегата для отображения (4 знака).

    Используется только для показа; округление НЕ меняет журнал/cash.
    Политика контекста задаётся явно, чтобы quantize не зависел от
    контекста вызывающего.
    """
    with localcontext() as ctx:
        _set_calc_policy(ctx)
        return value.quantize(DISPLAY_EXPONENT)


def quantity_of(value) -> Decimal:
    """Строка/Decimal/int → Decimal (точный), без float/округления."""
    if isinstance(value, Decimal) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise ValueError("нечисловое значение")


# --- Валидация величин и канонизация ---


def validate_metric(value, *, positive: bool, label: str) -> Decimal:
    """Валидация количества/цены/комиссии/баланса (F2.4/F2.5).

    Неконечные (NaN, inf) и вне диапазона значения отклоняются;
    `positive=True` → строго > 0, иначе >= 0.
    """
    try:
        dec = quantity_of(value)
    except (ValueError, InvalidOperation, DecimalException) as e:
        raise ValueError(
            f"{label}: нечисловое или некорректное значение ({e})"
        ) from e

    if not dec.is_finite():
        raise ValueError(f"{label}: значение должно быть конечным числом")
    if dec.is_nan():
        raise ValueError(f"{label}: NaN не допускается")
    if abs(dec) > MAX_VALUE:
        raise ValueError(f"{label}: значение вне допустимого диапазона")
    # заявленная точность хранения: после снятия НЕЗНАЧАЩИХ хвостовых
    # нулей должно остаться не более 30 целых и 18 дробных цифр.
    # 10^-20 и 10^-24 не представимы и отклоняются ДО записи;
    # '1.000000000000000000' представим — хвостовые нули незначащие.
    if canonical_text_for_stored(dec) is None:
        raise ValueError(
            f"{label}: значение вне заявленной точности хранения "
            "(максимум 30 цифр в целой части и 18 значащих — в дробной)"
        )
    if positive:
        if not dec > 0:
            raise ValueError(f"{label}: значение должно быть строго > 0")
    elif dec < 0:
        raise ValueError(f"{label}: значение не должно быть отрицательным")
    return dec


def canonical_amount(value, *, positive: bool, label: str) -> str:
    """Проверка + канонический текст Decimal (без незначащих нулей).

    Значение должно быть числом в допускаемой форме (десятичным,
    конечным, в границах точности) — возвращает канонический текст
    без ведущих нулей/экспоненты и без незначащих хвостовых нулей
    для записи в БД.
    """
    dec = validate_metric(value, positive=positive, label=label)
    return canonical_text_for_stored(dec)


def canonical_text_for_stored(value: Decimal):
    """Канонический текст значения ИЛИ None (не представимо точно).

    Канонизация — точные строковые операции над format_exact и не
    зависит от текущего Decimal-контекста: снимает только незначащие
    нули (ведущие в целой части, хвостовые в дробной; 10^-1-подобная
    экспонента разворачивается format_exact). НЕ использует округляющий
    normalize() — он в контексте 28 изменил бы большие точные значения.

    Возвращает None, если после снятия незначащих нулей значение
    выходит за границы хранения (больше 30 целых/18 дробных цифр) —
    запись невозможна без потери точности.
    """
    if not value.is_finite():
        return None
    text = format_exact(value)  # без экспоненты, независим от контекста
    neg = text.startswith("-")
    body = text[1:] if neg else text
    int_part, _, frac_part = body.partition(".")
    frac = frac_part.rstrip("0")  # хвостовые нули незначащие
    int_digits = int_part.lstrip("0") or "0"
    if len(int_digits) > MAX_INT_DIGITS or len(frac) > MAX_FRACTION_DIGITS:
        return None
    if int_digits == "0" and not frac:
        return "0"  # в том числе Decimal("-0")
    out = f"{int_digits}.{frac}" if frac else int_digits
    return f"-{out}" if neg else out


def validate_executed_time(executed_at, *, last=None):
    """Время операции: обязательно, не в будущем и не раньше последней.

    Наивное время трактуется как UTC; aware — сравнивается напрямую
    (Django хранит и возвращает UTC). Для идемпотентного повтора
    chronology-проверки пропускаются (см. execute_trade); нормализация
    наивного→UTC одинаковая и в снапшоте payload-хеша.
    """
    t = normalize_executed_time(executed_at)
    now = timezone.now()
    if t > now:
        raise ValueError("время операции не может быть в будущем")
    if last is not None:
        if timezone.is_naive(last):
            last = last.replace(tzinfo=datetime_tz.utc)
        if t < last:
            raise ValueError(
                "время операции не может быть раньше последней операции портфеля"
            )
    return t


def normalize_executed_time(executed_at):
    """Обязательное время операции; наивное трактуется как UTC."""
    if executed_at is None:
        raise ValueError("время операции обязательно")
    t = executed_at
    if timezone.is_naive(t):
        t = t.replace(tzinfo=datetime_tz.utc)
    return t


# --- Позиции из журнала ---


def compute_positions(portfolio) -> dict:
    """Позиции/себестоимость, реализованный P/L из журнала (F2.3/F2.7).

    Журнал — источник истины; позиция вычисляется на лету.

    Вычисления ведутся в широком контексте (CALC_PREC): входы
    допускаются ≤ 48 значащих цифр, умножения/сложения точны; деление
    (средняя себестоимость) округляется в этом контексте — нетривиальные
    дроби (1/3) точны быть не могут; отображение — display_round.

    {figi: {
        "open_qty": Decimal,
        "avg_cost": Decimal | None,
        "realized_pl": Decimal,
        "last_trade_time": datetime,
    }}
    """
    with derivations_context():
        result = {}
        trades = portfolio.trades.order_by("executed_at", "id")
        for tr in trades:
            figi = tr.figi
            item = result.setdefault(
                figi,
                {
                    "open_qty": Decimal(0),
                    "avg_cost": None,
                    "realized_pl": Decimal(0),
                    "last_trade_time": tr.executed_at,
                },
            )
            qty = tr.quantity_decimal
            price = tr.price_decimal
            commission = tr.commission_decimal
            if tr.side == Trade.SIDE_BUY:
                cost = qty * price + commission
                if item["open_qty"] == 0:
                    item["avg_cost"] = cost / qty
                else:
                    # пересчёт средней: включается комиссия покупки
                    old_cost = item["open_qty"] * item["avg_cost"]
                    item["avg_cost"] = (old_cost + cost) / (
                        item["open_qty"] + qty
                    )
                item["open_qty"] += qty
            else:  # SELL
                if qty > item["open_qty"]:
                    raise ValueError(
                        f"журнал непоследователен: продажа {qty} при остатке "
                        f"{item['open_qty']} (операция {tr.pk})"
                    )
                if item["avg_cost"] is None:
                    continue
                proceeds = qty * price - commission
                sold_cost = qty * item["avg_cost"]
                item["realized_pl"] += proceeds - sold_cost
                item["open_qty"] -= qty
                if item["open_qty"] == 0:
                    # полное закрытие — фиктивной позиции не остаётся
                    item["avg_cost"] = None
        for item in result.values():
            if item["open_qty"] == 0:
                # «Закрыт» — котировка не нужна
                item["avg_cost"] = None
        return result


def last_suitable_observation(
    share, source: str, currency: str, *, as_of=None
):
    """Последнее подходящее наблюдение для оценки (F2.6).

    Подходящее: источник и валюта (без регистра) совпадают с выбранными
    портфелем и инструментом, quote_time <= момента оценки.
    При равных временах выбор устойчив: quote_time, received_at, id.
    """
    from dashboard.models import PriceObservation

    if as_of is None:
        as_of = timezone.now()
    return (
        PriceObservation.objects.filter(
            share=share,
            source=source,
            currency__iexact=currency,
            quote_time__lte=as_of,
        )
        .order_by("-quote_time", "-received_at", "-id")
        .first()
    )


def position_rows(portfolio, *, as_of=None) -> dict:
    """Строчки состава: открытые позиции + оценка последней котировкой.

    Вычисления стоимости/себестоимости/P&L внутри каждой строки и их
    суммы — в точном контексте (exact_calc_context); окончательная
    прибыли/убытки по открытым позициям не пишутся в БД (что значит —
    допускает любой кредит в пределах вычисления), но исходные
    значения — qty/quote/cash — уже проверены валидатором.
    """
    from dashboard.models import Share

    positions = compute_positions(portfolio)
    cards = []
    # share_id известен у операций, записанных пока акция существовала;
    # SET_NULL оставил и снимок, но котировка ищется по живому объекту.
    share_ids = {
        t["share"]
        for t in portfolio.trades.values("share")
        if t["share"] is not None
    }
    share_by_figi = {s.figi: s for s in Share.objects.filter(id__in=share_ids)}
    incomplete = False
    market_value = Decimal(0)
    with derivations_context():
        for figi, pos in positions.items():
            if pos["open_qty"] == 0:
                continue
            share = share_by_figi.get(figi)
            obs = None
            if share is not None:
                obs = last_suitable_observation(
                    share,
                    portfolio.valuation_source,
                    share.currency,
                    as_of=as_of,
                )
            if obs is None:
                market_quote = None
                incomplete = True
            else:
                market_quote = obs.price_decimal
                market_value += pos["open_qty"] * market_quote
            cost = pos["open_qty"] * (pos["avg_cost"] or 0)
            unrealized = None
            position_value = None
            if market_quote is not None:
                # текущая стоимость позиции = количество × котировка
                position_value = pos["open_qty"] * market_quote
                unrealized = position_value - cost
            cards.append(
                {
                    "figi": figi,
                    "share": share,
                    "ticker": (share.ticker if share else ""),
                    "share_name": (share.name if share else ""),
                    "currency": share.currency if share else "",
                    "open_qty": pos["open_qty"],
                    "avg_cost": pos["avg_cost"],
                    "cost": cost,
                    "market_quote": market_quote,
                    "market_value": position_value,
                    "quote_source": obs.source if obs else None,
                    "quote_time": obs.quote_time if obs else None,
                    "unrealized_pl": unrealized,
                }
            )
    return {
        "cards": cards,
        "cash": portfolio.cash_decimal,
        "market_value": market_value,
        "incomplete": incomplete,
    }


def portfolio_summary(portfolio, *, as_of=None) -> dict:
    """Итоги портфеля (F2.3/F2.6): cash + рыночная стоимость + P/L.

    Честность итогов (F2.6): при неполной оценке (у какой-то открытой
    позиции нет подходящей котировки) полная оценка (total_value),
    совокупный P/L и доходность — None (не выглядят достоверным числом);
    известные части — cash и сумма оценённых позиций — показываются
    отдельно как частичные. Пустой портфель (без открытых позиций)
    полностью оцениваем: total = cash.
    """
    rows = position_rows(portfolio, as_of=as_of)
    cash = rows["cash"]
    market_value = rows["market_value"]
    with derivations_context():
        realized_pl = sum(
            (p["realized_pl"] for p in compute_positions(portfolio).values()),
            Decimal(0),
        )
        if rows["incomplete"]:
            # при неизвестной оценке хоть одной открытой позиции
            # нереализованный/совокупный P/L, полная оценка и доходность —
            # неизвестны, а не выглядят достоверным числом (F2.6)
            unrealized_pl = None
            overall_pl = None
            total_value = None
            return_pct = None
        else:
            unrealized_pl = sum(
                (
                    p["unrealized_pl"]
                    for p in rows["cards"]
                    if p["unrealized_pl"] is not None
                ),
                Decimal(0),
            )
            overall_pl = realized_pl + unrealized_pl
            total_value = cash + market_value
            start = portfolio.initial_cash_decimal
            return_pct = (overall_pl / start * 100) if start != 0 else None
        start = portfolio.initial_cash_decimal
        return {
            "cash": cash,
            "market_value": market_value,
            "total_value": total_value,
            "realized_pl": realized_pl,
            "unrealized_pl": unrealized_pl,
            "overall_pl": overall_pl,
            "return_pct": return_pct,
            "initial_cash": start,
            "incomplete": rows["incomplete"],
            "valuation_source": portfolio.valuation_source,
            "base_currency": portfolio.base_currency,
        }


# --- Манипуляции с балансом ---


def cash_delta_for(
    side: str, qty: Decimal, price: Decimal, commission: Decimal
) -> Decimal:
    """Дельта cash для операции (F2.3).

    Покупка: −(количество × цена + комиссия); продажа: +(количество ×
    цена − комиссия). Вызывается в точном контексте (exact_calc_context):
    результат либо точный, либо ошибки trap'ов — молчаливое округление
    недопустимо.
    """
    gross = qty * price
    if side == Trade.SIDE_BUY:
        return -(gross + commission)
    return gross - commission


def payload_hash(payload: dict) -> str:
    """Стабильный хеш полезной нагрузки для идемпотентности."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TradeValidationError(ValueError):
    """Явный отказ исполнения операции (без частичных изменений)."""


def trade_payload(share, side, qty, price_dec, comm, at, note) -> dict:
    """Снимок ядра операции для хеша идемпотентности.

    Величины канонизируются (без незначащих нулей): повтор с вводом
    '1.000000000000000000' и '1' — один и тот же payload.
    """
    return {
        "figi": share.figi,
        "side": side,
        "qty": canonical_text_for_stored(qty),
        "price": canonical_text_for_stored(price_dec),
        "commission": canonical_text_for_stored(comm),
        "executed_at": at.isoformat(),
        "note": note,
        "currency": share.currency,
    }


@transaction.atomic
def execute_trade(
    portfolio,
    *,
    side: str,
    share,
    quantity,
    price,
    commission=Decimal(0),
    executed_at=None,
    note="",
    idempotency_key=None,
) -> Trade:
    """Атомарное исполнение виртуальной операции (F2.3/F2.4/F2.8).

    Порядок проверок:
      1. валидация величин и инструмента;
      2. идемпотентность: существующий ключ + тот же payload → возврат
         прежней операции без записей/изменений cash (в т.ч. если
         после неё уже были новые операции — chronology-правила
         «не раньше последней» к повтору не применяются); тот же ключ
         с другим payload → отказ;
      3. chronology нового запроса: не в будущем и не раньше последней
         операции (новый ключ задним числом — отказ);
      4. достаточность денег/остатка; cash перечитывается из БД;
      5. проверка представимости результата (точность/границы) —
         ДО записи: если результат операции не может быть сохранён
         точно в заявленном диапазоне (≤ 30 целых + 18 дробных цифр),
         операция отклоняется целиком (ни cash, ни журнал не меняются);
      6. CAS-обновление cash: WHERE — точный текст, как он сейчас в БД
         (может быть неканоническим у старых записей), SET — канонический
         формат результата. Параллельный писатель делает 0 строк → отказ;
      7. запись операции; любой сбой откатывает и cash, и запись.
    """
    from dashboard.models import Share

    if not isinstance(share, Share):
        raise TradeValidationError("инструмент не найден")
    if side not in (Trade.SIDE_BUY, Trade.SIDE_SELL):
        raise TradeValidationError("неизвестная сторона операции")
    qty = validate_metric(quantity, positive=True, label="количество")
    price_dec = validate_metric(price, positive=True, label="цена")
    comm = validate_metric(commission, positive=False, label="комиссия")
    # величины уже проверены validate_metric: канонический текст (без
    # незначащих хвостовых нулей) существует и помещается в диапазон;
    # эти же тексты — в журнале и в snapshot хеша идемпотентности
    qty_text = canonical_text_for_stored(qty)
    price_text = canonical_text_for_stored(price_dec)
    comm_text = canonical_text_for_stored(comm)
    if qty_text is None or price_text is None or comm_text is None:
        raise TradeValidationError(
            "величина операции не представима без потери точности в "
            "заявленном диапазоне (до 30 целых и 18 дробных цифр)"
        )

    if share.currency.upper() != portfolio.base_currency.upper():
        raise TradeValidationError(
            "валюта инструмента не совпадает с валютой портфеля"
        )

    with exact_calc_context():
        # Идемпотентность ДО chronology: повтор той же операции должен
        # вернуть прежнюю запись даже после последующих операций,
        # «не раньше последней» к повтору не применяется.
        if idempotency_key:
            existing = Trade.objects.filter(
                portfolio=portfolio, idempotency_key=idempotency_key
            ).first()
            if existing:
                at0 = normalize_executed_time(executed_at)
                expected = payload_hash(
                    trade_payload(share, side, qty, price_dec, comm, at0, note)
                )
                if existing.payload_hash == expected:
                    return existing
                raise TradeValidationError(
                    "ключ идемпотентности уже использован с другими данными"
                )

        # Хронология нового запроса (не повтор)
        last_time = (
            Trade.objects.filter(portfolio=portfolio)
            .order_by("-executed_at", "-id")
            .values_list("executed_at", flat=True)
            .first()
        )
        at = validate_executed_time(executed_at, last=last_time)

        positions = compute_positions(portfolio)
        pos = positions.get(share.figi, {"open_qty": Decimal(0)})
        open_qty = pos["open_qty"]
        if side == Trade.SIDE_SELL and qty > open_qty:
            raise TradeValidationError(
                f"нельзя продать {qty} шт — доступно {open_qty}"
            )

        # cash перечитывается из БД — объект portfolio у вызывающего
        # может быть создан до предыдущих операций.
        fresh = Portfolio.objects.filter(id=portfolio.id).first()
        if fresh is None:
            raise TradeValidationError("портфель не найден")
        old_cash_text = fresh.cash  # точный текст, как он хранится сейчас
        old_cash = Decimal(old_cash_text)
        delta = cash_delta_for(side, qty, price_dec, comm)
        new_cash = old_cash + delta
        if new_cash < 0:
            raise TradeValidationError(
                "недостаточно денег: операция приведёт к отрицательному остатку"
            )
        # представимость результата ДО записи: точный результат должен
        # влезть в заявленный диапазон хранения без потери точности
        # (незначащие хвостовые нули снимаются: 999.000...0 → '999')
        new_cash_text = canonical_text_for_stored(new_cash)
        if new_cash_text is None:
            raise TradeValidationError(
                "результат операции не представим без потери точности "
                "в заявленном диапазоне (до 30 целых и 18 дробных цифр) — "
                "операция отклонена до записи без частичных изменений"
            )

        # CAS-обновление: WHERE — фактический текст в БД (даже
        # неканонический), SET — канонический текст результата.
        updated = Portfolio.objects.filter(
            id=portfolio.id, cash=old_cash_text
        ).update(cash=new_cash_text)
        if updated == 0:
            raise TradeValidationError(
                "конфликт параллельной операции — повторите попытку"
            )

        try:
            trade = Trade.objects.create(
                portfolio=portfolio,
                share=share,
                figi=share.figi,
                ticker=share.ticker,
                share_name=share.name,
                share_currency=share.currency,
                side=side,
                quantity=qty_text,
                price=price_text,
                commission=comm_text,
                currency=share.currency,
                note=note,
                executed_at=at,
                idempotency_key=idempotency_key or None,
                payload_hash=payload_hash(
                    trade_payload(share, side, qty, price_dec, comm, at, note)
                ),
            )
        except Exception as exc:
            # отказ записи откатывает транзакцию вместе с CAS-обновлением
            # cash — частичная операция невозможна (F2.8).
            raise TradeValidationError(
                f"запись операции не удалась: {exc}"
            ) from exc
        return trade
