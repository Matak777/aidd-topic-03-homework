from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.forms import StrategyForm
from dashboard.models import (
    Portfolio,
    PriceObservation,
    Share,
    Strategy,
    StrategyAttempt,
)
from dashboard.services.portfolio_accounting import execute_trade


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class StrategyUiCompletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "ui-owner@example.com"
        )
        self.other = get_user_model().objects.create_user(
            "ui-other@example.com"
        )
        self.portfolio = self.make_portfolio(self.user, "Owner portfolio")
        self.other_portfolio = self.make_portfolio(
            self.other, "Private portfolio"
        )
        self.share = Share.objects.create(
            figi="UI-FIGI",
            uid="UI-UID",
            isin="UI-ISIN",
            ticker="UI",
            name="UI share",
            currency="usd",
        )
        self.client.force_login(self.user)

    def make_portfolio(self, user, name):
        return Portfolio.objects.create(
            user=user,
            name=name,
            base_currency="USD",
            valuation_source="demo",
            initial_cash="1000",
            cash="1000",
        )

    def make_strategy(self, **changes):
        fields = dict(
            user=self.user,
            portfolio=self.portfolio,
            share=self.share,
            name="UI strategy",
            direction=">=",
            threshold="100",
            side="BUY",
            quantity="1",
            commission="0",
            source="demo",
            currency="USD",
            enabled=True,
        )
        fields.update(changes)
        return Strategy.objects.create(**fields)

    def payload(self, **changes):
        values = dict(
            portfolio=str(self.portfolio.pk),
            share=str(self.share.pk),
            name="Saved UI strategy",
            direction=">=",
            threshold="100",
            side="BUY",
            quantity="1",
            commission="0",
            source="demo",
            currency="USD",
            enabled="on",
        )
        values.update(changes)
        return values

    def attempt(self, strategy, decision="executed", reason=""):
        observation = PriceObservation.objects.create(
            share=self.share,
            price="110",
            source="demo",
            currency="USD",
            quote_time=timezone.now() - timedelta(seconds=1),
        )
        trade = None
        if decision == "executed":
            trade = execute_trade(
                strategy.portfolio,
                share=self.share,
                side="BUY",
                quantity="1",
                price="110",
                commission="0",
                executed_at=timezone.now(),
                idempotency_key=f"ui-attempt-{observation.pk}",
            )
        return StrategyAttempt.objects.create(
            user=strategy.user,
            strategy=strategy,
            portfolio=strategy.portfolio,
            observation=observation,
            trade=trade,
            strategy_name=strategy.name,
            figi=self.share.figi,
            ticker=self.share.ticker,
            share_name=self.share.name,
            share_currency=self.share.currency,
            portfolio_name=strategy.portfolio.name,
            portfolio_currency="USD",
            direction=">=",
            threshold="100",
            source="demo",
            currency="USD",
            side="BUY",
            quantity="1",
            commission="0",
            observed_price="110",
            quote_time=observation.quote_time,
            decision=decision,
            reason=reason,
        )

    def test_edit_page_exists_and_invalid_post_keeps_saved_strategy(self):
        strategy = self.make_strategy()
        url = reverse("dashboard:strategy-edit", args=[strategy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сохранить стратегию")
        self.assertContains(response, strategy.name)
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        strategy.refresh_from_db()
        self.assertEqual(strategy.name, "UI strategy")
        self.assertEqual(strategy.cursor_id, 0)

    def test_list_reports_executed_and_rejected_counts_on_get_and_form_error(
        self,
    ):
        strategy = self.make_strategy()
        self.attempt(strategy)
        self.attempt(strategy)
        self.attempt(
            strategy, decision="rejected", reason="Недостаточно денег"
        )
        for response in (
            self.client.get(reverse("dashboard:strategies")),
            self.client.post(reverse("dashboard:strategies"), {}),
        ):
            self.assertEqual(response.status_code, 200)
            rows = response.context["strategies"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1]["executed"], 2)
            self.assertEqual(rows[0][1]["rejected"], 1)
            self.assertContains(response, "успешно: 2")
            self.assertContains(response, "отказов: 1")

    def test_form_rejects_foreign_portfolio_and_incompatible_instrument(self):
        foreign = StrategyForm(
            self.payload(portfolio=str(self.other_portfolio.pk)),
            user=self.user,
        )
        self.assertFalse(foreign.is_valid())
        self.assertIn("portfolio", foreign.errors)
        euro = Share.objects.create(
            figi="EUR-FIGI",
            uid="EUR-UID",
            isin="EUR-ISIN",
            ticker="EURUI",
            name="EUR share",
            currency="EUR",
        )
        mismatch = StrategyForm(
            self.payload(share=str(euro.pk)), user=self.user
        )
        self.assertFalse(mismatch.is_valid())
        self.assertIn("share", mismatch.errors)
        valid = StrategyForm(self.payload(currency="usd"), user=self.user)
        self.assertTrue(valid.is_valid(), valid.errors)

    def test_form_rejects_source_currency_and_nonpositive_amounts(self):
        for field, value in (
            ("source", "tinkoff"),
            ("currency", "RUB"),
            ("quantity", "0"),
            ("threshold", "-1"),
            ("commission", "-1"),
        ):
            with self.subTest(field=field):
                form = StrategyForm(
                    self.payload(**{field: value}), user=self.user
                )
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_create_and_edit_baseline_failures_roll_back_parameters(self):
        form = StrategyForm(self.payload(), user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        with patch(
            "dashboard.services.strategy_runner.reset_baseline",
            side_effect=RuntimeError("baseline failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "baseline failure"):
                form.apply_strategy(self.user)
        self.assertFalse(Strategy.objects.exists())

        strategy = self.make_strategy()
        edited = StrategyForm(
            self.payload(name="Changed", threshold="120"), user=self.user
        )
        self.assertTrue(edited.is_valid(), edited.errors)
        with patch(
            "dashboard.services.strategy_runner.reset_baseline",
            side_effect=RuntimeError("baseline failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "baseline failure"):
                edited.apply_strategy(self.user, instance=strategy)
        strategy.refresh_from_db()
        self.assertEqual(strategy.name, "UI strategy")
        self.assertEqual(strategy.threshold, "100")
        self.assertFalse(self.portfolio.trades.exists())

    def test_enabling_rolls_back_if_baseline_cannot_be_saved(self):
        strategy = self.make_strategy(enabled=False)
        with patch(
            "dashboard.services.strategy_runner.reset_baseline",
            side_effect=RuntimeError("baseline failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "baseline failure"):
                self.client.post(
                    reverse("dashboard:strategy-toggle", args=[strategy.pk])
                )
        strategy.refresh_from_db()
        self.assertFalse(strategy.enabled)

    def test_old_attempt_links_to_original_trade_portfolio_after_move(self):
        strategy = self.make_strategy()
        attempt = self.attempt(strategy)
        old_portfolio_id = self.portfolio.pk
        new_portfolio = self.make_portfolio(self.user, "New portfolio")
        response = self.client.post(
            reverse("dashboard:strategy-edit", args=[strategy.pk]),
            self.payload(
                portfolio=str(new_portfolio.pk), name="Moved strategy"
            ),
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(
            reverse("dashboard:strategy-attempts", args=[strategy.pk])
        )
        expected = reverse(
            "dashboard:portfolio-journal", args=[old_portfolio_id]
        )
        self.assertContains(
            response, f'href="{expected}#trade-{attempt.trade_id}"'
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.portfolio_id, old_portfolio_id)
        self.assertEqual(attempt.portfolio_name, "Owner portfolio")
        self.assertEqual(attempt.strategy_name, "UI strategy")

    def test_deleted_strategy_and_share_snapshots_remain_in_owned_global_journal(
        self,
    ):
        strategy = self.make_strategy()
        attempt = self.attempt(strategy)
        self.client.post(
            reverse("dashboard:strategy-delete", args=[strategy.pk])
        )
        self.client.post(reverse("dashboard:delete", args=[self.share.pk]))
        attempt.refresh_from_db()
        self.assertIsNone(attempt.strategy_id)
        self.assertIsNone(attempt.observation_id)
        self.assertEqual(attempt.threshold, "100")
        self.assertEqual(attempt.observed_price, "110")
        self.assertIsNotNone(attempt.trade_id)
        response = self.client.get(reverse("dashboard:strategy-attempts-all"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "UI strategy")
        self.assertContains(response, "стратегия удалена")
        self.assertContains(response, "DEMO")
        self.client.force_login(self.other)
        response = self.client.get(reverse("dashboard:strategy-attempts-all"))
        self.assertNotContains(response, "UI strategy")
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_foreign_strategy_is_unreadable_and_cannot_be_mutated(self):
        strategy = self.make_strategy()
        self.client.force_login(self.other)
        for name in ("strategy-edit", "strategy-attempts"):
            self.assertEqual(
                self.client.get(
                    reverse("dashboard:" + name, args=[strategy.pk])
                ).status_code,
                404,
            )
        for name in ("strategy-edit", "strategy-toggle", "strategy-delete"):
            self.assertEqual(
                self.client.post(
                    reverse("dashboard:" + name, args=[strategy.pk]), {}
                ).status_code,
                404,
            )
        strategy.refresh_from_db()
        self.assertTrue(strategy.enabled)

    def test_mutations_require_csrf_and_get_does_not_change_state(self):
        strategy = self.make_strategy()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        for name, args in (
            ("strategies", []),
            ("strategy-edit", [strategy.pk]),
            ("strategy-toggle", [strategy.pk]),
            ("strategy-delete", [strategy.pk]),
        ):
            self.assertEqual(
                csrf.post(
                    reverse("dashboard:" + name, args=args), {}
                ).status_code,
                403,
            )
        for name in ("strategy-toggle", "strategy-delete"):
            self.assertEqual(
                csrf.get(
                    reverse("dashboard:" + name, args=[strategy.pk])
                ).status_code,
                405,
            )
        strategy.refresh_from_db()
        self.assertTrue(strategy.enabled)
        self.assertEqual(strategy.cursor_id, 0)

    def test_portfolio_with_strategy_has_clear_protected_delete_message(self):
        self.make_strategy()
        response = self.client.post(
            reverse("dashboard:portfolio-delete", args=[self.portfolio.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "связаны стратегии")
        self.assertTrue(
            Portfolio.objects.filter(pk=self.portfolio.pk).exists()
        )

    def test_portfolio_valuation_is_honest_and_manual_origin_is_distinct(self):
        strategy = self.make_strategy()
        execute_trade(
            self.portfolio,
            share=self.share,
            side="BUY",
            quantity="1",
            price="100",
            commission="0",
            executed_at=timezone.now(),
            idempotency_key="ui-manual",
        )
        response = self.client.get(
            reverse("dashboard:strategy-attempts", args=[strategy.pk])
        )
        self.assertContains(response, "не изолированная доходность")
        self.assertContains(response, "Неизвестна: нет котировки")
        journal = self.client.get(
            reverse("dashboard:portfolio-journal", args=[self.portfolio.pk])
        )
        self.assertContains(journal, "Ручная цена виртуальной сделки")
        attempt = self.attempt(strategy)
        journal = self.client.get(
            reverse("dashboard:portfolio-journal", args=[self.portfolio.pk])
        )
        self.assertContains(journal, f'id="trade-{attempt.trade_id}"')
        self.assertContains(journal, "Автоматическая виртуальная операция")
        self.assertContains(journal, "DEMO")
