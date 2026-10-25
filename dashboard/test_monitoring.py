from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from dashboard.models import (
    PriceAlert,
    PriceObservation,
    Share,
    Watchlist,
    WatchlistItem,
    WatchRule,
)
from dashboard.services.price_alerts import (
    initialize_new_rule_state,
    process_price_alerts,
    reset_rule_state,
)


User = get_user_model()
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_share(**overrides):
    fields = {
        "figi": "FIGI-MONITOR",
        "isin": "ISIN-MONITOR",
        "name": "Monitor share",
        "ticker": "MON",
        "currency": "usd",
        "uid": "UID-MONITOR",
    }
    fields.update(overrides)
    return Share.objects.create(**fields)


class PriceAlertServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="monitor@example.com",
            email="monitor@example.com",
            password="safe-password",
        )
        self.watchlist = Watchlist.objects.create(owner=self.user, name="Мой список")
        self.share = make_share()
        self.item = WatchlistItem.objects.create(
            watchlist=self.watchlist,
            share=self.share,
        )

    def add_observation(
        self,
        price,
        seconds,
        *,
        source=PriceObservation.Source.DEMO,
        currency="usd",
    ):
        return PriceObservation.objects.create(
            share=self.share,
            price=Decimal(str(price)),
            currency=currency,
            source=source,
            observed_at=BASE_TIME + timedelta(seconds=seconds),
            received_at=BASE_TIME + timedelta(seconds=seconds + 1),
        )

    def make_rule(
        self,
        *,
        direction=WatchRule.Direction.AT_LEAST,
        threshold="100",
        source=PriceObservation.Source.DEMO,
        currency="usd",
    ):
        rule = WatchRule.objects.create(
            item=self.item,
            source=source,
            currency=currency,
            direction=direction,
            threshold=Decimal(threshold),
        )
        initialize_new_rule_state(rule)
        rule.refresh_from_db()
        return rule

    def test_gte_equality_fires_once_and_alert_keeps_event_snapshot(self):
        rule = self.make_rule()
        self.add_observation("100.000000000", 1)

        self.assertEqual(process_price_alerts(), 1)
        self.assertEqual(process_price_alerts(), 0)
        alert = PriceAlert.objects.get()
        self.assertEqual(alert.price, Decimal("100.000000000"))
        self.assertEqual(alert.threshold, Decimal("100"))
        self.assertEqual(alert.direction, WatchRule.Direction.AT_LEAST)
        self.assertEqual(alert.source, PriceObservation.Source.DEMO)
        self.assertEqual(alert.currency, "usd")
        self.assertEqual(alert.observed_at, BASE_TIME + timedelta(seconds=1))

        rule.threshold = Decimal("250")
        rule.save(update_fields=("threshold", "updated_at"))
        alert.refresh_from_db()
        self.assertEqual(alert.threshold, Decimal("100"))
        self.assertEqual(alert.price, Decimal("100.000000000"))

    def test_lte_equality_fires_for_first_new_observation(self):
        self.make_rule(
            direction=WatchRule.Direction.AT_MOST,
            threshold="50",
        )
        self.add_observation("50", 1)

        self.assertEqual(process_price_alerts(), 1)
        alert = PriceAlert.objects.get()
        self.assertEqual(alert.direction, WatchRule.Direction.AT_MOST)

    def test_new_rule_skips_old_matching_history_but_first_new_match_fires(self):
        self.add_observation("110", 1)
        rule = self.make_rule()
        self.assertTrue(rule.armed)
        self.assertEqual(rule.cursor_observation_id, self.share.price_observations.get().pk)
        self.assertEqual(process_price_alerts(), 0)

        self.add_observation("120", 2)

        self.assertEqual(process_price_alerts(), 1)
        self.assertEqual(PriceAlert.objects.count(), 1)
        self.assertEqual(PriceAlert.objects.get().price, Decimal("120"))
        rule.refresh_from_db()
        self.assertFalse(rule.armed)

    def test_creation_skips_old_nonmatching_history_and_lte_first_match_fires(self):
        self.add_observation("60", 1)
        rule = self.make_rule(
            direction=WatchRule.Direction.AT_MOST,
            threshold="50",
        )
        self.assertTrue(rule.armed)
        self.assertEqual(process_price_alerts(), 0)
        self.add_observation("50", 2)

        self.assertEqual(process_price_alerts(), 1)
        self.assertEqual(PriceAlert.objects.get().price, Decimal("50"))

    def test_edit_and_reenable_reset_to_latest_without_replaying_it(self):
        rule = self.make_rule()
        self.add_observation("110", 1)
        process_price_alerts()
        self.assertEqual(PriceAlert.objects.count(), 1)

        rule.threshold = Decimal("105")
        rule.save(update_fields=("threshold", "updated_at"))
        reset_rule_state(rule)
        rule.refresh_from_db()
        self.assertFalse(rule.armed)
        self.assertEqual(process_price_alerts(), 0)

        rule.enabled = False
        rule.save(update_fields=("enabled", "updated_at"))
        self.add_observation("90", 2)
        self.assertEqual(process_price_alerts(), 0)

        rule.enabled = True
        rule.save(update_fields=("enabled", "updated_at"))
        reset_rule_state(rule)
        self.assertTrue(rule.armed)
        self.add_observation("105", 3)
        self.assertEqual(process_price_alerts(), 1)

    def test_backlog_is_processed_in_order_and_late_older_event_is_ignored(self):
        rule = self.make_rule()
        self.add_observation("110", 3)
        self.add_observation("90", 4)
        self.add_observation("110", 5)
        self.assertEqual(process_price_alerts(), 2)
        rule.refresh_from_db()
        cursor_id = rule.cursor_observation_id

        late = PriceObservation.objects.create(
            share=self.share,
            price=Decimal("90"),
            currency="usd",
            source=PriceObservation.Source.DEMO,
            observed_at=BASE_TIME + timedelta(seconds=2),
            received_at=BASE_TIME + timedelta(seconds=10),
        )
        self.assertEqual(process_price_alerts(), 0)
        rule.refresh_from_db()
        self.assertNotEqual(rule.cursor_observation_id, late.pk)
        self.assertEqual(rule.cursor_observation_id, cursor_id)
        self.assertEqual(PriceAlert.objects.count(), 2)
        self.assertFalse(rule.armed)

    def test_other_source_and_currency_do_not_change_rule_state(self):
        rule = self.make_rule()
        self.add_observation("110", 1, source=PriceObservation.Source.TINKOFF)
        self.add_observation("110", 2, currency="rub")
        self.assertEqual(process_price_alerts(), 0)
        rule.refresh_from_db()
        self.assertTrue(rule.armed)
        self.assertIsNone(rule.cursor_observed_at)

    def test_currency_matching_is_case_insensitive_without_changing_snapshots(self):
        rule = self.make_rule(currency="usd")
        self.add_observation("101", 1, currency="USD")

        self.assertEqual(process_price_alerts(), 1)
        alert = PriceAlert.objects.get()
        self.assertEqual(rule.currency, "usd")
        self.assertEqual(alert.currency, "USD")

    def test_no_history_first_match_fires_and_reprocessing_is_idempotent(self):
        self.make_rule()
        self.add_observation("101", 1)
        self.assertEqual(process_price_alerts(), 1)
        self.assertEqual(process_price_alerts(), 0)
        self.assertEqual(PriceAlert.objects.count(), 1)

    @override_settings(PRICE_DATA_MODE="demo")
    def test_local_price_update_collects_demo_observation_and_alert(self):
        self.make_rule()

        from dashboard.tasks import _update_last_price

        self.assertEqual(_update_last_price(share_id=self.share.pk), 1)
        self.assertEqual(PriceObservation.objects.count(), 1)
        alert = PriceAlert.objects.get()
        self.assertEqual(alert.source, PriceObservation.Source.DEMO)
        self.assertIn("Демонстрационные", alert.get_source_display())


class WatchlistViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="safe-password",
        )
        self.other = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="safe-password",
        )
        self.watchlist = Watchlist.objects.create(owner=self.owner, name="Owner list")
        self.share = make_share(
            figi="FIGI-VIEW",
            isin="ISIN-VIEW",
            ticker="VIEW",
            uid="UID-VIEW",
        )
        self.item = WatchlistItem.objects.create(
            watchlist=self.watchlist,
            share=self.share,
        )
        self.rule = WatchRule.objects.create(
            item=self.item,
            source=PriceObservation.Source.DEMO,
            currency="usd",
            direction=WatchRule.Direction.AT_LEAST,
            threshold=Decimal("100"),
        )
        reset_rule_state(self.rule)
        self.alert = PriceAlert.objects.create(
            owner=self.owner,
            rule=self.rule,
            observation=PriceObservation.objects.create(
                share=self.share,
                price=Decimal("101"),
                currency="usd",
                source=PriceObservation.Source.DEMO,
                observed_at=BASE_TIME,
                received_at=BASE_TIME,
            ),
            share_ticker="VIEW",
            share_name="View share",
            price=Decimal("101"),
            threshold=Decimal("100"),
            direction=WatchRule.Direction.AT_LEAST,
            source=PriceObservation.Source.DEMO,
            currency="usd",
            observed_at=BASE_TIME,
        )

    def test_owner_can_view_lists_rules_and_demo_alert(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard:watchlists"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Owner list")

        response = self.client.get(
            reverse("dashboard:watchlist-detail", args=(self.watchlist.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VIEW")

        response = self.client.get(reverse("dashboard:price-alerts"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ДЕМО")
        self.assertContains(response, "101")

    def test_anonymous_user_is_redirected_to_application_login(self):
        response = self.client.get(reverse("dashboard:watchlists"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/user/login/?next=/watchlists/")

    def test_empty_post_forms_return_validation_errors(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(reverse("dashboard:watchlists"), {}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                reverse("dashboard:watch-rule-edit", args=(self.rule.pk,)),
                {},
            ).status_code,
            400,
        )

    def test_foreign_watchlist_rule_and_alert_are_not_accessible(self):
        self.client.force_login(self.other)
        paths = (
            reverse("dashboard:watchlist-detail", args=(self.watchlist.pk,)),
            reverse("dashboard:watch-rule-edit", args=(self.rule.pk,)),
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse("dashboard:watch-rule-toggle", args=(self.rule.pk,)),
                {"enabled": "false"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("dashboard:price-alert-read", args=(self.alert.pk,))
            ).status_code,
            404,
        )
        self.assertTrue(WatchRule.objects.get(pk=self.rule.pk).enabled)
        self.assertFalse(PriceAlert.objects.get(pk=self.alert.pk).is_read)

    def test_rule_creation_and_edit_are_post_only_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        detail_url = reverse("dashboard:watchlist-detail", args=(self.watchlist.pk,))
        self.assertEqual(client.post(detail_url, {}).status_code, 403)
        self.assertEqual(
            client.get(reverse("dashboard:watch-rule-toggle", args=(self.rule.pk,))).status_code,
            405,
        )

    def test_editing_rule_resets_baseline_and_mark_read_is_owner_scoped(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:watch-rule-edit", args=(self.rule.pk,)),
            {
                "source": PriceObservation.Source.DEMO,
                "currency": "usd",
                "direction": WatchRule.Direction.AT_LEAST,
                "threshold": "101",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.armed)
        self.assertEqual(self.rule.cursor_observation_id, self.alert.observation_id)

        response = self.client.post(
            reverse("dashboard:price-alert-read", args=(self.alert.pk,))
        )
        self.assertEqual(response.status_code, 302)
        self.alert.refresh_from_db()
        self.assertTrue(self.alert.is_read)
        self.assertEqual(PriceAlert.objects.count(), 1)

    @override_settings(PRICE_DATA_MODE="demo")
    def test_user_can_create_rule_for_uppercase_usd_and_collect_alert(self):
        self.client.force_login(self.owner)
        self.share.currency = "USD"
        self.share.save(update_fields=("currency", "updated_at"))
        response = self.client.post(
            reverse("dashboard:watchlists"),
            {"name": "Новый список"},
        )
        self.assertEqual(response.status_code, 302)
        watchlist = Watchlist.objects.get(owner=self.owner, name="Новый список")
        response = self.client.post(
            reverse("dashboard:watchlist-detail", args=(watchlist.pk,)),
            {
                "action": "add-item",
                "share": self.share.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        item = watchlist.items.get()
        old_observation = PriceObservation.objects.create(
            share=self.share,
            price=Decimal("110"),
            currency="USD",
            source=PriceObservation.Source.DEMO,
            observed_at=BASE_TIME + timedelta(seconds=1),
            received_at=BASE_TIME + timedelta(seconds=1),
        )
        response = self.client.post(
            reverse("dashboard:watchlist-detail", args=(watchlist.pk,)),
            {
                "action": "add-rule",
                "item_id": item.pk,
                "source": PriceObservation.Source.DEMO,
                "currency": "usd",
                "direction": WatchRule.Direction.AT_LEAST,
                "threshold": "100",
            },
        )
        self.assertEqual(response.status_code, 302)
        rule = item.rules.get()
        self.assertEqual(rule.threshold, Decimal("100"))
        self.assertEqual(rule.currency, "usd")
        self.assertTrue(rule.state_initialized)
        self.assertTrue(rule.armed)
        self.assertEqual(rule.cursor_observation_id, old_observation.pk)

        from dashboard.tasks import _update_last_price

        self.assertEqual(_update_last_price(share_id=self.share.pk), 1)
        observation = PriceObservation.objects.filter(
            observed_at__gt=old_observation.observed_at
        ).latest("observed_at")
        alert = PriceAlert.objects.get(rule=rule)
        self.assertEqual(observation.currency, "USD")
        self.assertEqual(alert.currency, "USD")
        self.assertEqual(alert.price, Decimal("110.250000000"))
        self.assertEqual(alert.source, PriceObservation.Source.DEMO)

    def test_html_share_delete_preserves_alert_snapshot_and_removes_market_rows(self):
        self.client.force_login(self.owner)
        observation = self.alert.observation

        response = self.client.post(reverse("dashboard:delete", args=(self.share.pk,)))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Share.objects.filter(pk=self.share.pk).exists())
        self.assertFalse(PriceObservation.objects.filter(pk=observation.pk).exists())
        self.assertFalse(WatchlistItem.objects.filter(pk=self.item.pk).exists())
        self.assertFalse(WatchRule.objects.filter(pk=self.rule.pk).exists())
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.observation_id)
        self.assertIsNone(self.alert.rule_id)
        self.assertEqual(self.alert.share_ticker, "VIEW")
        self.assertEqual(self.alert.share_name, "View share")
        self.assertEqual(self.alert.price, Decimal("101"))
        self.assertEqual(self.alert.currency, "usd")

    def test_list_membership_is_unique_removable_and_cannot_use_foreign_item(self):
        self.client.force_login(self.owner)
        another_list = Watchlist.objects.create(owner=self.other, name="Other list")
        foreign_item = WatchlistItem.objects.create(
            watchlist=another_list,
            share=self.share,
        )
        url = reverse("dashboard:watchlist-detail", args=(self.watchlist.pk,))
        response = self.client.post(
            url,
            {
                "action": "add-rule",
                "item_id": foreign_item.pk,
                "source": PriceObservation.Source.DEMO,
                "currency": "usd",
                "direction": WatchRule.Direction.AT_LEAST,
                "threshold": "100",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(WatchRule.objects.filter(item=foreign_item).exists())

        response = self.client.post(
            reverse("dashboard:watchlist-item-delete", args=(self.item.pk,))
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WatchlistItem.objects.filter(pk=self.item.pk).exists())
        self.assertFalse(WatchRule.objects.filter(pk=self.rule.pk).exists())
        self.alert.refresh_from_db()
        self.assertIsNone(self.alert.rule_id)
        self.assertEqual(self.alert.price, Decimal("101"))
