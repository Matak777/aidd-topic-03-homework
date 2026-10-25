from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from dashboard.forms import ShareModelForm
from dashboard.models import (
    PortfolioPosition,
    PortfolioTransaction,
    PriceObservation,
    Share,
    StrategyEvaluation,
    TradingStrategy,
    VirtualPortfolio,
)
from dashboard.services.portfolios import execute_portfolio_transaction
from dashboard.services.virtual_strategies import (
    initialize_strategy,
    process_virtual_strategies,
)


User = get_user_model()


class VirtualStrategyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="strategy-owner@example.com",
            email="strategy-owner@example.com",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            username="strategy-other@example.com",
            email="strategy-other@example.com",
            password="test-password",
        )
        self.share = self.make_share()
        self.portfolio = self.make_portfolio(self.user)

    def make_share(self, **overrides):
        values = {
            "figi": f"STRATEGY-{uuid4().hex[:8]}",
            "isin": "STRATEGY-ISIN",
            "name": "Strategy instrument",
            "ticker": "STRAT",
            "currency": "USD",
            "uid": f"STRATEGY-UID-{uuid4().hex[:8]}",
        }
        values.update(overrides)
        return Share.objects.create(**values)

    def make_portfolio(self, owner, *, cash="1000"):
        return VirtualPortfolio.objects.create(
            owner=owner,
            name=f"Strategy portfolio {uuid4().hex[:6]}",
            currency="USD",
            valuation_source=PriceObservation.Source.DEMO,
            starting_cash=Decimal(cash),
            cash_balance=Decimal(cash),
        )

    def make_strategy(
        self,
        *,
        direction=TradingStrategy.Direction.AT_LEAST,
        action=TradingStrategy.Action.BUY,
        threshold="100",
        quantity="1",
        commission="0",
        portfolio=None,
        share=None,
        initialize=True,
    ):
        share = share or self.share
        strategy = TradingStrategy.objects.create(
            owner=self.user,
            portfolio=portfolio or self.portfolio,
            share=share,
            name=f"Strategy {uuid4().hex[:6]}",
            share_ticker=share.ticker,
            share_name=share.name,
            source=PriceObservation.Source.DEMO,
            currency="USD",
            direction=direction,
            threshold=Decimal(threshold),
            action=action,
            quantity=Decimal(quantity),
            commission=Decimal(commission),
        )
        if initialize:
            initialize_strategy(strategy, new_strategy=True)
        return strategy

    def add_observation(
        self,
        price,
        *,
        share=None,
        source=PriceObservation.Source.DEMO,
        currency="USD",
        observed_at=None,
        received_at=None,
    ):
        observed_at = observed_at or timezone.now()
        return PriceObservation.objects.create(
            share=share or self.share,
            price=Decimal(price),
            currency=currency,
            source=source,
            observed_at=observed_at,
            received_at=received_at or timezone.now(),
        )

    def add_manual_position(self, quantity="2", price="50"):
        return execute_portfolio_transaction(
            portfolio_id=self.portfolio.pk,
            share_id=self.share.pk,
            side=PortfolioTransaction.Side.BUY,
            quantity=Decimal(quantity),
            execution_price=Decimal(price),
            commission=Decimal("0"),
            currency="USD",
            executed_at=timezone.now() - timedelta(minutes=2),
            idempotency_key=uuid4(),
        )[0]

    def test_create_view_ignores_satisfying_history_but_first_new_price_triggers(self):
        self.add_observation("110")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("dashboard:strategies"),
            {
                "name": "Buy above threshold",
                "portfolio": self.portfolio.pk,
                "share": self.share.pk,
                "source": PriceObservation.Source.DEMO,
                "currency": "usd",
                "direction": TradingStrategy.Direction.AT_LEAST,
                "threshold": "100",
                "action": TradingStrategy.Action.BUY,
                "quantity": "1",
                "commission": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        strategy = TradingStrategy.objects.get(name="Buy above threshold")
        self.assertTrue(strategy.armed)
        self.assertFalse(strategy.evaluations.exists())
        new_observation = self.add_observation("120")
        self.assertEqual(process_virtual_strategies(share_id=self.share.pk), 1)
        self.assertEqual(process_virtual_strategies(share_id=self.share.pk), 0)
        transaction_record = PortfolioTransaction.objects.get(
            execution_observation=new_observation
        )
        self.assertEqual(transaction_record.execution_type, "strategy")
        self.assertEqual(transaction_record.quote_source, "demo")
        self.assertEqual(transaction_record.quote_currency, "USD")
        self.assertEqual(strategy.evaluations.get().decision, "executed")
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_future_observation_does_not_poison_create_baseline(self):
        future = timezone.now() + timedelta(days=1)
        self.add_observation("110", observed_at=future, received_at=timezone.now())
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("dashboard:strategies"),
            {
                "name": "Future-safe create",
                "portfolio": self.portfolio.pk,
                "share": self.share.pk,
                "source": "demo",
                "currency": "USD",
                "direction": "gte",
                "threshold": "100",
                "action": "buy",
                "quantity": "1",
                "commission": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        strategy = TradingStrategy.objects.get(name="Future-safe create")
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertTrue(strategy.armed)

        current = self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            PortfolioTransaction.objects.get().execution_observation_id,
            current.pk,
        )

    def test_gte_equality_repeat_suppression_and_rearming(self):
        strategy = self.make_strategy()
        observations = [
            ("99", StrategyEvaluation.Decision.NO_TRIGGER),
            ("100", StrategyEvaluation.Decision.EXECUTED),
            ("120", StrategyEvaluation.Decision.NO_TRIGGER),
            ("99", StrategyEvaluation.Decision.REARMED),
            ("100", StrategyEvaluation.Decision.EXECUTED),
        ]
        for price, _decision in observations:
            self.add_observation(price)
            process_virtual_strategies(share_id=self.share.pk)

        self.assertEqual(
            list(strategy.evaluations.values_list("decision", flat=True)),
            [decision for _price, decision in observations],
        )
        self.assertEqual(
            PortfolioTransaction.objects.filter(execution_type="strategy").count(),
            2,
        )

    def test_lte_equality_executes_sell_and_preserves_manual_origin(self):
        self.add_manual_position()
        strategy = self.make_strategy(
            direction=TradingStrategy.Direction.AT_MOST,
            action=TradingStrategy.Action.SELL,
        )
        self.add_observation("101")
        process_virtual_strategies(share_id=self.share.pk)
        self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)

        self.assertEqual(
            strategy.evaluations.order_by("pk").last().decision,
            StrategyEvaluation.Decision.EXECUTED,
        )
        position = PortfolioPosition.objects.get(portfolio=self.portfolio, share=self.share)
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertEqual(
            PortfolioTransaction.objects.filter(execution_type="strategy").count(),
            1,
        )
        self.assertEqual(
            PortfolioTransaction.objects.filter(execution_type="manual").count(),
            1,
        )

    def test_rejected_attempt_consumes_trigger_until_condition_exit(self):
        self.portfolio.cash_balance = Decimal("10")
        self.portfolio.save(update_fields=("cash_balance",))
        strategy = self.make_strategy()
        for price in ("100", "110"):
            self.add_observation(price)
            process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            list(strategy.evaluations.values_list("decision", flat=True)),
            [
                StrategyEvaluation.Decision.REJECTED,
                StrategyEvaluation.Decision.NO_TRIGGER,
            ],
        )
        self.assertEqual(PortfolioTransaction.objects.count(), 0)

        self.add_observation("99")
        process_virtual_strategies(share_id=self.share.pk)
        self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(strategy.evaluations.filter(decision="rejected").count(), 2)

    def test_sell_without_position_is_rejected_without_ledger_changes(self):
        strategy = self.make_strategy(
            direction=TradingStrategy.Direction.AT_MOST,
            action=TradingStrategy.Action.SELL,
        )
        self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)

        evaluation = strategy.evaluations.get()
        self.portfolio.refresh_from_db()
        self.assertEqual(evaluation.decision, StrategyEvaluation.Decision.REJECTED)
        self.assertIn("открытого количества", evaluation.reason)
        self.assertEqual(self.portfolio.cash_balance, Decimal("1000.000000000"))
        self.assertFalse(PortfolioPosition.objects.exists())
        self.assertFalse(PortfolioTransaction.objects.exists())

    def test_edit_uses_current_satisfying_price_as_baseline(self):
        self.add_observation("110")
        strategy = self.make_strategy()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("dashboard:strategy-edit", args=(strategy.pk,)),
            {
                "name": strategy.name,
                "portfolio": self.portfolio.pk,
                "share": self.share.pk,
                "source": "demo",
                "currency": "USD",
                "direction": "gte",
                "threshold": "100",
                "action": "buy",
                "quantity": "1",
                "commission": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        strategy.refresh_from_db()
        self.assertFalse(strategy.armed)

        self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(PortfolioTransaction.objects.count(), 0)
        self.add_observation("99")
        process_virtual_strategies(share_id=self.share.pk)
        self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_future_observation_does_not_poison_edit_baseline(self):
        strategy = self.make_strategy()
        self.add_observation(
            "110",
            observed_at=timezone.now() + timedelta(days=1),
            received_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("dashboard:strategy-edit", args=(strategy.pk,)),
            {
                "name": strategy.name,
                "portfolio": self.portfolio.pk,
                "share": self.share.pk,
                "source": "demo",
                "currency": "USD",
                "direction": "gte",
                "threshold": "100",
                "action": "buy",
                "quantity": "1",
                "commission": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        strategy.refresh_from_db()
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertTrue(strategy.armed)

        current = self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            PortfolioTransaction.objects.get().execution_observation_id,
            current.pk,
        )

    def test_reenable_uses_new_baseline_without_retroactive_trade(self):
        strategy = self.make_strategy()
        self.client.force_login(self.user)
        self.client.post(reverse("dashboard:strategy-toggle", args=(strategy.pk,)))
        self.add_observation("110")
        self.client.post(reverse("dashboard:strategy-toggle", args=(strategy.pk,)))
        strategy.refresh_from_db()
        self.assertFalse(strategy.armed)
        self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(PortfolioTransaction.objects.count(), 0)

    def test_future_observation_does_not_poison_reenable_baseline(self):
        strategy = self.make_strategy()
        self.client.force_login(self.user)
        self.client.post(reverse("dashboard:strategy-toggle", args=(strategy.pk,)))
        self.add_observation(
            "110",
            observed_at=timezone.now() + timedelta(days=1),
            received_at=timezone.now(),
        )
        self.client.post(reverse("dashboard:strategy-toggle", args=(strategy.pk,)))

        strategy.refresh_from_db()
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertTrue(strategy.armed)
        current = self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            PortfolioTransaction.objects.get().execution_observation_id,
            current.pk,
        )

    def test_stale_and_future_points_are_journaled_without_blocking_fresh_price(self):
        strategy = self.make_strategy()
        now = timezone.now()
        self.add_observation("110", observed_at=now - timedelta(minutes=10))
        self.add_observation("120", observed_at=now)
        self.add_observation("130", observed_at=now + timedelta(minutes=1))

        process_virtual_strategies(share_id=self.share.pk)

        decisions = list(
            strategy.evaluations.order_by("observed_at").values_list("decision", flat=True)
        )
        self.assertEqual(
            decisions,
            [
                StrategyEvaluation.Decision.SKIPPED,
                StrategyEvaluation.Decision.EXECUTED,
                StrategyEvaluation.Decision.SKIPPED,
            ],
        )
        self.assertEqual(PortfolioTransaction.objects.count(), 1)
        self.assertIn("устарела", strategy.evaluations.order_by("observed_at").first().reason)

    def test_future_and_stale_observations_do_not_change_state_or_block_valid_price(self):
        strategy = self.make_strategy()
        starting_cash = self.portfolio.cash_balance
        future = self.add_observation(
            "120",
            observed_at=timezone.now() + timedelta(days=1),
            received_at=timezone.now() + timedelta(seconds=1),
        )
        stale = self.add_observation(
            "120",
            observed_at=timezone.now() - timedelta(minutes=10),
        )

        process_virtual_strategies(observation_id=future.pk)
        process_virtual_strategies(observation_id=stale.pk)
        strategy.refresh_from_db()
        self.portfolio.refresh_from_db()
        self.assertTrue(strategy.armed)
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertEqual(self.portfolio.cash_balance, starting_cash)
        self.assertFalse(PortfolioPosition.objects.exists())
        self.assertFalse(PortfolioTransaction.objects.exists())

        current = self.add_observation("120")
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            PortfolioTransaction.objects.get().execution_observation_id,
            current.pk,
        )

    def test_late_observation_is_logged_but_does_not_rewind_cursor(self):
        strategy = self.make_strategy()
        recent = self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)
        late = self.add_observation(
            "90",
            observed_at=recent.observed_at - timedelta(minutes=1),
        )
        process_virtual_strategies(share_id=self.share.pk)

        late_evaluation = strategy.evaluations.get(observation_key=late.pk)
        strategy.refresh_from_db()
        self.assertEqual(late_evaluation.decision, StrategyEvaluation.Decision.SKIPPED)
        self.assertEqual(strategy.cursor_observation_id, recent.pk)
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_currency_and_source_are_never_mixed(self):
        strategy = self.make_strategy()
        initial_armed = strategy.armed
        mismatched_currency = self.add_observation(
            "100",
            currency="RUB",
            observed_at=timezone.now() - timedelta(seconds=1),
        )
        process_virtual_strategies(
            share_id=self.share.pk,
            observation_id=mismatched_currency.pk,
        )
        strategy.refresh_from_db()
        self.assertEqual(
            strategy.evaluations.get(observation_key=mismatched_currency.pk).decision,
            StrategyEvaluation.Decision.SKIPPED,
        )
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertEqual(strategy.armed, initial_armed)
        current = self.add_observation(
            "100",
            currency="usd",
            observed_at=timezone.now() - timedelta(seconds=2),
        )
        process_virtual_strategies(share_id=self.share.pk)
        self.assertEqual(
            PortfolioTransaction.objects.get().execution_observation_id,
            current.pk,
        )

        other_strategy = self.make_strategy()
        wrong_source = self.add_observation(
            "100",
            source=PriceObservation.Source.TINKOFF,
            received_at=timezone.now() + timedelta(seconds=1),
        )
        process_virtual_strategies(
            observation_id=wrong_source.pk,
        )
        self.assertEqual(
            other_strategy.evaluations.get(observation_key=wrong_source.pk).decision,
            StrategyEvaluation.Decision.SKIPPED,
        )
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_old_execution_link_uses_transaction_portfolio_after_strategy_move(self):
        strategy = self.make_strategy()
        observation = self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)
        transaction_record = PortfolioTransaction.objects.get(
            execution_observation=observation
        )
        second_portfolio = self.make_portfolio(self.user)

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("dashboard:strategy-edit", args=(strategy.pk,)),
            {
                "name": strategy.name,
                "portfolio": second_portfolio.pk,
                "share": self.share.pk,
                "source": "demo",
                "currency": "USD",
                "direction": "gte",
                "threshold": "100",
                "action": "buy",
                "quantity": "1",
                "commission": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        detail = self.client.get(
            reverse("dashboard:strategy-detail", args=(strategy.pk,))
        )
        self.assertEqual(detail.status_code, 200)
        old_transaction_link = (
            f'href="{reverse("dashboard:portfolio-detail", args=(transaction_record.portfolio_id,))}'
            f'#transaction-{transaction_record.pk}"'
        )
        self.assertContains(detail, old_transaction_link)
        self.assertNotEqual(transaction_record.portfolio_id, second_portfolio.pk)

    def test_share_identity_and_currency_are_protected_by_strategy_reference(self):
        strategy = self.make_strategy()
        original = {
            "figi": self.share.figi,
            "uid": self.share.uid,
            "ticker": self.share.ticker,
            "currency": self.share.currency,
        }
        self.client.force_login(self.user)

        api = APIClient()
        api.force_authenticate(self.user)
        response = api.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"figi": "REPLACEMENT-FIGI", "uid": "REPLACEMENT-UID", "ticker": "NEW"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

        form = ShareModelForm(
            data={
                "figi": "REPLACEMENT-FIGI",
                "isin": self.share.isin,
                "name": self.share.name,
                "ticker": "NEW",
                "currency": self.share.currency,
                "uid": "REPLACEMENT-UID",
                "last_price": "",
                "is_trend_high": "",
            },
            instance=self.share,
        )
        self.assertFalse(form.is_valid())
        self.assertTrue(
            any("стратегия" in str(error) for errors in form.errors.values() for error in errors)
        )

        html_response = self.client.post(
            reverse("dashboard:update", args=(self.share.pk,)),
            {
                "figi": "REPLACEMENT-FIGI",
                "isin": self.share.isin,
                "name": self.share.name,
                "ticker": "NEW",
                "currency": self.share.currency,
                "uid": "REPLACEMENT-UID",
                "last_price": "",
                "is_trend_high": "",
            },
        )
        self.assertEqual(html_response.status_code, 200)

        currency_response = api.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"currency": "RUB"},
            format="json",
        )
        self.assertEqual(currency_response.status_code, 400)
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, original["figi"])
        self.assertEqual(self.share.uid, original["uid"])
        self.assertEqual(self.share.ticker, original["ticker"])
        self.assertEqual(self.share.currency, original["currency"])

        rename_response = api.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"name": "Renamed without identity change"},
            format="json",
        )
        self.assertEqual(rename_response.status_code, 200)
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Renamed without identity change")
        self.assertTrue(TradingStrategy.objects.filter(pk=strategy.pk).exists())

    def test_share_catalog_import_refuses_identity_change_with_strategy(self):
        strategy = self.make_strategy()
        original_figi = self.share.figi
        original_uid = self.share.uid
        remote_share = SimpleNamespace(
            figi=original_figi,
            uid="CATALOG-REPLACEMENT-UID",
            isin=self.share.isin,
            name="Catalog replacement",
            ticker="CATNEW",
            currency="USD",
        )
        api_client = MagicMock()
        api_client.__enter__.return_value = api_client
        api_client.__exit__.return_value = False
        api_client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=api_client,
        ):
            with self.assertRaises(CommandError):
                call_command("add_shares")

        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, original_figi)
        self.assertEqual(self.share.ticker, "STRAT")
        self.assertEqual(self.share.uid, original_uid)
        self.assertTrue(TradingStrategy.objects.filter(pk=strategy.pk).exists())

    def test_foreign_instrument_observation_is_skipped_with_or_without_share_filter(self):
        strategy = self.make_strategy()
        other_share = self.make_share(
            figi="OTHER-STRATEGY-FIGI",
            uid="OTHER-STRATEGY-UID",
            ticker="OTHER",
        )
        other_observation = self.add_observation(
            "150",
            share=other_share,
            observed_at=timezone.now(),
        )
        initial_cash = self.portfolio.cash_balance

        self.assertEqual(
            process_virtual_strategies(
                share_id=self.share.pk,
                observation_id=other_observation.pk,
            ),
            1,
        )
        strategy.refresh_from_db()
        self.assertTrue(strategy.armed)
        self.assertIsNone(strategy.cursor_observed_at)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash_balance, initial_cash)
        self.assertFalse(PortfolioTransaction.objects.exists())
        self.assertEqual(
            strategy.evaluations.get(observation_key=other_observation.pk).decision,
            StrategyEvaluation.Decision.SKIPPED,
        )

        another_observation = self.add_observation(
            "151",
            share=other_share,
            observed_at=timezone.now() + timedelta(seconds=1),
        )
        process_virtual_strategies(observation_id=another_observation.pk)
        strategy.refresh_from_db()
        self.assertTrue(strategy.armed)
        self.assertIsNone(strategy.cursor_observed_at)
        self.assertFalse(PortfolioTransaction.objects.exists())

    def test_portfolio_service_rejects_inconsistent_strategy_provenance(self):
        strategy = self.make_strategy()
        other_share = self.make_share(
            figi="SERVICE-OTHER-FIGI",
            uid="SERVICE-OTHER-UID",
            ticker="SVCOTHER",
        )
        observation = self.add_observation("150", share=other_share)

        with self.assertRaisesMessage(ValidationError, "другому инструменту"):
            execute_portfolio_transaction(
                portfolio_id=self.portfolio.pk,
                share_id=self.share.pk,
                side=PortfolioTransaction.Side.BUY,
                quantity=strategy.quantity,
                execution_price=observation.price,
                commission=strategy.commission,
                currency=observation.currency,
                executed_at=timezone.now(),
                idempotency_key=uuid4(),
                execution_type=PortfolioTransaction.ExecutionType.STRATEGY,
                strategy=strategy,
                strategy_name=strategy.name,
                execution_observation=observation,
            )

        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash_balance, Decimal("1000.000000000"))
        self.assertFalse(PortfolioPosition.objects.exists())
        self.assertFalse(PortfolioTransaction.objects.exists())

    def test_multiple_strategies_execute_in_stable_order_on_one_observation(self):
        first = self.make_strategy()
        second = self.make_strategy()
        observation = self.add_observation("100")

        process_virtual_strategies(share_id=self.share.pk)

        transactions = list(
            PortfolioTransaction.objects.filter(execution_observation=observation)
            .order_by("pk")
            .values_list("strategy_id", flat=True)
        )
        self.assertEqual(transactions, [first.pk, second.pk])
        self.assertEqual(
            StrategyEvaluation.objects.filter(
                observation_key=observation.pk,
                decision=StrategyEvaluation.Decision.EXECUTED,
            ).count(),
            2,
        )

    def test_foreign_strategy_and_portfolio_are_not_accessible_or_selectable(self):
        foreign_portfolio = self.make_portfolio(self.other_user)
        strategy = self.make_strategy(portfolio=foreign_portfolio)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse("dashboard:strategy-detail", args=(strategy.pk,))
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("dashboard:strategies"),
            {
                "name": "Foreign portfolio attempt",
                "portfolio": foreign_portfolio.pk,
                "share": self.share.pk,
                "source": "demo",
                "currency": "USD",
                "direction": "gte",
                "threshold": "100",
                "action": "buy",
                "quantity": "1",
                "commission": "0",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            TradingStrategy.objects.filter(name="Foreign portfolio attempt").exists()
        )

    def test_html_mutations_require_post_and_csrf(self):
        strategy = self.make_strategy()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        detail_url = reverse("dashboard:strategy-detail", args=(strategy.pk,))
        self.assertEqual(client.get(detail_url).status_code, 200)
        self.assertEqual(
            client.post(reverse("dashboard:strategy-toggle", args=(strategy.pk,))).status_code,
            403,
        )
        token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("dashboard:strategy-toggle", args=(strategy.pk,)),
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 302)
        strategy.refresh_from_db()
        self.assertFalse(strategy.enabled)

    def test_strategy_deletion_keeps_evaluation_and_transaction_snapshots(self):
        strategy = self.make_strategy()
        observation = self.add_observation("100")
        process_virtual_strategies(share_id=self.share.pk)
        evaluation = strategy.evaluations.get(observation_key=observation.pk)
        transaction_record = evaluation.transaction

        self.client.force_login(self.user)
        self.client.post(reverse("dashboard:strategy-delete", args=(strategy.pk,)))

        evaluation.refresh_from_db()
        transaction_record.refresh_from_db()
        self.assertIsNone(evaluation.strategy_id)
        self.assertIsNone(transaction_record.strategy_id)
        self.assertEqual(evaluation.strategy_name, strategy.name)
        self.assertEqual(transaction_record.strategy_name, strategy.name)
        self.assertEqual(transaction_record.quote_source, "demo")
        self.assertEqual(transaction_record.quote_observed_at, observation.observed_at)
        self.assertEqual(transaction_record.execution_type, "strategy")

        response = self.client.post(
            reverse("dashboard:delete", args=(self.share.pk,))
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Share.objects.filter(pk=self.share.pk).exists())
        evaluation.refresh_from_db()
        transaction_record.refresh_from_db()
        self.assertIsNone(evaluation.observation_id)
        self.assertIsNone(evaluation.share_id)
        self.assertEqual(evaluation.observed_price, Decimal("100"))
        self.assertIsNone(transaction_record.execution_observation_id)
        self.assertIsNone(transaction_record.share_id)
        self.assertEqual(transaction_record.share_ticker, "STRAT")
        self.assertEqual(transaction_record.quote_source, "demo")

    @override_settings(PRICE_DATA_MODE="demo")
    def test_local_collector_creates_one_strategy_trade_without_external_order(self):
        strategy = self.make_strategy(threshold="0")
        from dashboard.tasks import _update_last_price

        self.assertEqual(_update_last_price(share_id=self.share.pk), 1)
        self.assertEqual(_update_last_price(share_id=self.share.pk), 1)

        transaction_record = PortfolioTransaction.objects.get(
            execution_type=PortfolioTransaction.ExecutionType.STRATEGY
        )
        self.assertEqual(transaction_record.execution_observation.source, "demo")
        self.assertEqual(transaction_record.quote_source, "demo")
        self.assertEqual(strategy.evaluations.filter(decision="executed").count(), 1)
        self.assertEqual(
            PortfolioTransaction.objects.filter(execution_type="strategy").count(),
            1,
        )
        self.share.refresh_from_db()
        self.assertIsNone(self.share.last_price)
