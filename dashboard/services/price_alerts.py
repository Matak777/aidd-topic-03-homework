from django.db import transaction
from django.db.models import Q

from dashboard.models import PriceAlert, PriceObservation, WatchRule


def is_rule_satisfied(rule, price):
    if rule.direction == WatchRule.Direction.AT_LEAST:
        return price >= rule.threshold
    return price <= rule.threshold


def reset_rule_state(rule):
    latest = (
        PriceObservation.objects.filter(
            share=rule.item.share,
            source=rule.source,
            currency__iexact=rule.currency,
        )
        .order_by("-observed_at", "-pk")
        .first()
    )
    if latest is None:
        rule.cursor_observed_at = None
        rule.cursor_observation_id = None
        rule.armed = True
    else:
        rule.cursor_observed_at = latest.observed_at
        rule.cursor_observation_id = latest.pk
        rule.armed = not is_rule_satisfied(rule, latest.price)
    rule.state_initialized = True
    rule.save(
        update_fields=(
            "cursor_observed_at",
            "cursor_observation_id",
            "armed",
            "state_initialized",
            "updated_at",
        )
    )


def initialize_new_rule_state(rule):
    latest = (
        PriceObservation.objects.filter(
            share=rule.item.share,
            source=rule.source,
            currency__iexact=rule.currency,
        )
        .order_by("-observed_at", "-pk")
        .first()
    )
    if latest is None:
        rule.cursor_observed_at = None
        rule.cursor_observation_id = None
    else:
        rule.cursor_observed_at = latest.observed_at
        rule.cursor_observation_id = latest.pk
    rule.armed = True
    rule.state_initialized = True
    rule.save(
        update_fields=(
            "cursor_observed_at",
            "cursor_observation_id",
            "armed",
            "state_initialized",
            "updated_at",
        )
    )


def _observations_after_cursor(rule):
    observations = PriceObservation.objects.filter(
        share=rule.item.share,
        source=rule.source,
        currency__iexact=rule.currency,
    )
    if rule.cursor_observed_at is not None:
        observations = observations.filter(
            Q(observed_at__gt=rule.cursor_observed_at)
            | Q(
                observed_at=rule.cursor_observed_at,
                pk__gt=rule.cursor_observation_id,
            )
        )
    return observations.order_by("observed_at", "pk")


def process_price_alerts(*, share_id=None):
    rule_ids = WatchRule.objects.filter(enabled=True).values_list("pk", flat=True)
    if share_id is not None:
        rule_ids = rule_ids.filter(item__share_id=share_id)

    created_count = 0
    for rule_id in rule_ids.iterator():
        created_count += _process_rule(rule_id)
    return created_count


@transaction.atomic
def _process_rule(rule_id):
    rule = (
        WatchRule.objects.select_for_update()
        .select_related("item__watchlist__owner", "item__share")
        .filter(pk=rule_id, enabled=True)
        .first()
    )
    if rule is None:
        return 0

    if not rule.state_initialized:
        initialize_new_rule_state(rule)
        return 0

    created_count = 0
    for observation in _observations_after_cursor(rule).iterator():
        satisfied = is_rule_satisfied(rule, observation.price)
        if rule.armed and satisfied:
            _alert, created = PriceAlert.objects.get_or_create(
                rule=rule,
                observation=observation,
                defaults={
                    "owner": rule.item.watchlist.owner,
                    "share_ticker": rule.item.share.ticker,
                    "share_name": rule.item.share.name,
                    "price": observation.price,
                    "threshold": rule.threshold,
                    "direction": rule.direction,
                    "source": observation.source,
                    "currency": observation.currency,
                    "observed_at": observation.observed_at,
                },
            )
            created_count += int(created)
            rule.armed = False
        elif not rule.armed and not satisfied:
            rule.armed = True

        rule.cursor_observed_at = observation.observed_at
        rule.cursor_observation_id = observation.pk

    rule.save(
        update_fields=(
            "armed",
            "cursor_observed_at",
            "cursor_observation_id",
            "updated_at",
        )
    )
    return created_count
