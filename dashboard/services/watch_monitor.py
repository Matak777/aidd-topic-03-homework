"""Персональный мониторинг акций (этап 3, группа 1 FEATURES.md).

Общий сервис проверки правил и создания уведомлений: представления HTML,
локальный коллектор `collect_prices` и задача Celery
`update_last_price` (оба через общий путь записи `apply_prices`)
используют одни и те же функции. Сервис выделяется так, чтобы будущие
стратегии (этап 4) применяли те же условия — сами стратегии в этом
этапе не реализуются.

Политика событий (объявлена в FEATURES.md §1):

- совместимое наблюдение: тот же инструмент; источник совпадает точно;
  валюта без регистра; цена — точный Decimal из `price_decimal`
  (не Float-кеш и не цена виртуальной сделки); quote_time не позже
  момента проверки (будущие точки состояние не меняют);
- при создании/редактировании/включении (`reset_baseline`) актуальная
  совместимая небудущая цена становится исходным состоянием БЕЗ
  уведомления; старая история не проигрывается (курсор);
- срабатывание только на новые совместимые наблюдения (id > курсора);
  пока условие выполнено — повторов нет; наблюдение вне условия
  повторно взводит; окна обрабатываются по порядку (время, id);
  запоздавшая точка (head_quote_time отсекает) не откатывает состояние;
- атомарность: состояние/курсор/голова и уведомление меняются в одной
  транзакции; дубли исключены уникальностью (правило, наблюдение) —
  она переживает параллельных обработчиков и перезапуски; она же
  НЕ запрещает повторное событие после выхода/входа (это другое
  наблюдение);
- согласованно с этапом 2 используется общий валидатор точности
  (`validate_metric`): порог строго > 0, до 30 целых и 18 значащих
  дробных цифр, канонический текст.
"""

import logging
from decimal import InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.models import (
    PriceObservation,
    WatchEvent,
    WatchRule,
)
from dashboard.services.portfolio_accounting import canonical_amount

logger = logging.getLogger(__name__)


def _compatible_price(rule, obs):
    """Совместимое наблюдение → точный Decimal цены, иначе None.

    Источник — точно; валюта — без регистра; цена должна быть
    конечным Decimal (некорректная цена не меняет состояние).
    Время котировки проверяется отдельно (только на момент обработки).
    """
    if obs.share_id != rule.share_id:
        return None
    if obs.source != rule.source:
        return None
    if (obs.currency or "").upper() != rule.currency.upper():
        return None
    try:
        price = obs.price_decimal
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price.is_nan():
        return None
    return price


def _matches(rule, price) -> bool:
    """Условие правила: ≥/≤; равенство порогу удовлетворяет."""
    threshold = rule.threshold_decimal
    if rule.direction == WatchRule.DIRECTION_GTE:
        return price >= threshold
    return price <= threshold


def reset_baseline(rule, *, on_change=False) -> None:
    """Актуальная совместимая небудущая цена — исходное состояние.

    Создание (`on_change=False`) — старая история не проигрывается:
    исходное состояние всегда `armed`, первое НОВОЕ совместимое
    наблюдение уведомляет, даже если старая последняя цена уже была
    подходящей. Редактирование/включение (`on_change=True`) — если
    актуальная цена уже удовлетворяет, состояние `triggered` БЕЗ
    уведомления (перед новым срабатыванием нужен выход); данных нет —
    `armed` (ждём первое новое наблюдение).
    """
    now = timezone.now()
    rule.cursor_id = (
        PriceObservation.objects.filter(share=rule.share)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
        or 0
    )
    head = None
    state = WatchRule.STATE_ARMED
    candidates = PriceObservation.objects.filter(
        share=rule.share,
        source=rule.source,
        currency__iexact=rule.currency,
        quote_time__lte=now,
    ).order_by("-quote_time", "-id")
    # последнее ПРИГОДНОЕ наблюдение: новейшая точка с некорректной
    # ценой не сбрасывает baseline (иначе «лишние» уведомления после
    # исправления ошибки данных). Фильтры источника/валюты/
    # небудущего времени и семантика создания сохраняются.
    for candidates_obs in candidates:
        price = _compatible_price(rule, candidates_obs)
        if price is None:
            continue
        head = candidates_obs.quote_time
        if not on_change:
            state = WatchRule.STATE_ARMED
        else:
            state = (
                WatchRule.STATE_TRIGGERED
                if _matches(rule, price)
                else WatchRule.STATE_ARMED
            )
        break
    rule.head_quote_time = head
    rule.state = state
    rule.save()


def create_rule(
    user, *, share, direction, threshold_text, source, currency, enabled=True
):
    """Создание правила с базовым состоянием (без проигрывания истории)."""
    threshold_text = canonical_amount(
        threshold_text, positive=True, label="порог"
    )
    rule = WatchRule.objects.create(
        user=user,
        share=share,
        direction=direction,
        threshold=threshold_text,
        source=source,
        currency=currency,
        enabled=enabled,
    )
    # создание: старая история не проигрывается, первое новое
    # наблюдение уведомляет
    reset_baseline(rule)
    return rule


def _event_snapshot(rule, obs, price) -> dict:
    """Снимок события: изменение/удаление правила не перепишет его."""
    share = rule.share
    message = (
        f"{share.ticker}: цена {price} {rule.direction} "
        f"{rule.threshold} ({rule.source}/{rule.currency})"
    )
    return {
        "user": rule.user,
        "watch_rule": rule,
        "observation": obs,
        "figi": share.figi,
        "ticker": share.ticker,
        "share_name": share.name,
        "share_currency": share.currency,
        "direction": rule.direction,
        "threshold": rule.threshold,
        "source": rule.source,
        "currency": rule.currency,
        "observed_price": str(price),
        "quote_time": obs.quote_time,
        "message": message,
    }


@transaction.atomic
def process_rule(rule, *, now=None) -> int:
    """Атомарная обработка окна новых наблюдений одного правила.

    Возвращает число созданных уведомлений. Строка правила блокируется
    (select_for_update): параллельные обработчики сериализуются, а
    уникальность (правило, наблюдение) подстраховывает даже при
    гонках — SQLite сериализует запись движком; состояние правила,
    курсор, голова и уведомление меняются одной транзакцией.
    """
    locked = WatchRule.objects.select_for_update().get(id=rule.id)
    if not locked.enabled:
        return 0
    if now is None:
        now = timezone.now()
    created = 0
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

    # устойчивый порядок: по времени котировки и идентификатору
    candidates.sort(key=lambda item: (item[0], item[1]))
    head = locked.head_quote_time
    state = locked.state
    events = []
    for quote_time, _obs_id, obs, price in candidates:
        # запоздавшая точка (время раньше применённого состояния):
        # не откатывает состояние и не создаёт ретроспективного события
        if head is not None and obs.quote_time < head:
            continue
        satisfied = _matches(locked, price)
        if state == WatchRule.STATE_TRIGGERED:
            if satisfied:
                # условие всё ещё выполнено — повторов нет
                pass
            else:
                # выход повторно взводит правило
                state = WatchRule.STATE_ARMED
        elif satisfied:
            events.append((obs, price))
            state = WatchRule.STATE_TRIGGERED
        head = obs.quote_time

    locked.cursor_id = scanned_max
    locked.head_quote_time = head
    locked.state = state
    locked.save()
    for obs, price in events:
        # Уникальность (правило, наблюдение) — единственный признак
        # дубля: ошибка записи считается дублем ТОЛЬКО после
        # подтверждения существующей записи той же пары. Иной сбой
        # (например, реальный DB-триггер RAISE(ABORT)) откатывает всю
        # попытку обработки правила — состояние/курсор/голова выше
        # входят в ту же транзакцию — чтобы наблюдение можно было
        # обработать повторно после устранения ошибки.
        try:
            with transaction.atomic():
                WatchEvent.objects.create(
                    **_event_snapshot(locked, obs, price)
                )
                created += 1
        except IntegrityError as e:
            is_duplicate = WatchEvent.objects.filter(
                watch_rule=locked, observation=obs, user=locked.user
            ).exists()
            if is_duplicate:
                # событие уже создал параллельный обработчик:
                # идемпотентный повтор, обновлённое состояние сохранено
                continue
            # настоящий сбой записи: откат всего process_rule (включая
            # state/cursor/head) и проброс наружу — процесс-коллектор
            # журнализует и продолжит со следующего правила
            logger.warning(
                f"watch_monitor: rule {locked.id}: event persist "
                f"failed (observation {obs.id}): {e}"
            )
            raise
    return created


def process_share(share, *, now=None) -> int:
    """Проверка всех включённых правил инструмента.

    Автоматическая проверка подключена к общему пути записи наблюдений
    (`apply_prices`): локальный коллектор и Celery-задача проверяют
    правила сами. Сбой одного правила журнализуется и не прерывает
    остальные правила и остальной цикл записи.
    """
    total = 0
    for rule in WatchRule.objects.filter(share=share, enabled=True):
        try:
            total += process_rule(rule, now=now)
        except Exception as e:
            logger.warning(f"watch_monitor: rule {rule.id} failed: {e}")
    return total
