"""Service regressions added while completing the interrupted strategy stage."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import TestCase
from django.utils import timezone

from dashboard.models import (
    Portfolio,
    PriceObservation,
    Share,
    Strategy,
    Trade,
)
from dashboard.services import portfolio_accounting, strategy_runner


class StrategyServiceCompletionTests(TestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.user = get_user_model().objects.create_user(
            "service@example.com", "service@example.com", "test-password"
        )
        self.other = get_user_model().objects.create_user(
            "other-service@example.com",
            "other-service@example.com",
            "test-password",
        )
        self.share = Share.objects.create(
            figi="SERVICE-ASSET",
            isin="SERVICE-ASSET",
            uid="SERVICE-ASSET",
            ticker="SRVC",
            name="Service asset",
            currency="usd",
        )
        self.portfolio = Portfolio.objects.create(
            user=self.user,
            name="Service portfolio",
            base_currency="USD",
            valuation_source="demo",
            initial_cash="1000",
            cash="1000",
        )
        self.quote_start = timezone.now() - timedelta(hours=1)
        self.quote_index = 0

    def strategy(self, **changes):
        values = {
            "user": self.user,
            "portfolio": self.portfolio,
            "share": self.share,
            "name": "Service strategy",
            "direction": ">=",
            "threshold": "100",
            "side": "BUY",
            "quantity": "1",
            "commission": "0",
            "source": "demo",
            "currency": "USD",
        }
        values.update(changes)
        strategy = Strategy.objects.create(**values)
        strategy_runner.reset_baseline(strategy)
        return strategy

    def observation(self, price, **changes):
        self.quote_index += 1
        values = {
            "share": self.share,
            "price": price,
            "currency": "USD",
            "source": "demo",
            "quote_time": self.quote_start
            + timedelta(seconds=self.quote_index),
        }
        values.update(changes)
        return PriceObservation.objects.create(**values)

    def assert_no_financial_changes(self, portfolio=None):
        portfolio = portfolio or self.portfolio
        portfolio.refresh_from_db()
        self.assertEqual(portfolio.cash, "1000")
        self.assertFalse(portfolio.trades.exists())

    def assert_rule_unprocessed(self, strategy):
        strategy.refresh_from_db()
        self.assertEqual(strategy.state, "armed")
        self.assertEqual(strategy.cursor_id, 0)
        self.assertIsNone(strategy.head_quote_time)
        self.assertFalse(strategy.attempts.exists())

    def test_control_100_then_105_is_one_trade_and_five_profit(self):
        strategy = self.strategy()
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.observation("105")
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.portfolio.refresh_from_db()
        summary = portfolio_accounting.portfolio_summary(self.portfolio)
        self.assertEqual(self.portfolio.cash, "900")
        self.assertEqual(summary["total_value"], Decimal("1005"))
        self.assertEqual(summary["overall_pl"], Decimal("5"))
        self.assertEqual(strategy.attempts.count(), 1)
        self.assertEqual(self.portfolio.trades.count(), 1)

    def test_buy_and_sell_include_commissions_and_preserve_quote_time(self):
        buy = self.strategy(quantity="2", commission="1")
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(buy), 1)
        sell = self.strategy(
            name="Sell",
            direction="<=",
            threshold="110",
            side="SELL",
            commission="2",
        )
        quote = self.observation("105")
        self.assertEqual(strategy_runner.process_strategy(sell), 1)
        attempt = sell.attempts.get()
        self.portfolio.refresh_from_db()
        position = portfolio_accounting.compute_positions(self.portfolio)[
            self.share.figi
        ]
        self.assertEqual(self.portfolio.cash, "902")
        self.assertEqual(position["open_qty"], Decimal("1"))
        self.assertEqual(position["realized_pl"], Decimal("2.5"))
        self.assertEqual(attempt.quote_time, quote.quote_time)
        self.assertGreater(attempt.trade.executed_at, quote.quote_time)

    def test_insufficient_cash_refusal_consumes_entry_until_rearmed(self):
        strategy = self.strategy()
        self.observation("1100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        refusal = strategy.attempts.get()
        self.assertEqual(refusal.decision, "rejected")
        self.assertIn("недостаточно", refusal.reason)
        self.assertIsNone(refusal.trade_id)
        self.assert_no_financial_changes()
        self.observation("1200")
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.observation("90")
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash, "900")
        self.assertEqual(strategy.attempts.count(), 2)
        self.assertEqual(self.portfolio.trades.count(), 1)

    def test_sell_without_shares_is_a_persisted_refusal(self):
        strategy = self.strategy(direction="<=", side="SELL")
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        attempt = strategy.attempts.get()
        self.assertEqual(attempt.decision, "rejected")
        self.assertIn("нельзя продать", attempt.reason)
        self.assert_no_financial_changes()

    def test_foreign_portfolio_cannot_be_traded_by_persisted_strategy(self):
        self.portfolio.user = self.other
        self.portfolio.save()
        strategy = self.strategy()
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        attempt = strategy.attempts.get()
        self.assertEqual(attempt.decision, "rejected")
        self.assertIn("Владелец", attempt.reason)
        self.assert_no_financial_changes()

    def test_demo_quote_cannot_trade_a_tinkoff_portfolio(self):
        self.portfolio.valuation_source = "tinkoff"
        self.portfolio.save()
        strategy = self.strategy()
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.assertEqual(strategy.attempts.get().decision, "rejected")
        self.assertIn("Источник", strategy.attempts.get().reason)
        self.assert_no_financial_changes()

    def test_strategy_share_and_portfolio_currencies_must_all_match(self):
        for target in ("strategy", "share", "portfolio"):
            with self.subTest(target=target):
                strategy = self.strategy()
                if target == "strategy":
                    strategy.currency = "EUR"
                    strategy.save()
                elif target == "share":
                    self.share.currency = "EUR"
                    self.share.save()
                else:
                    self.portfolio.base_currency = "EUR"
                    self.portfolio.save()
                self.observation("100", currency=strategy.currency)
                self.assertEqual(strategy_runner.process_strategy(strategy), 1)
                attempt = strategy.attempts.get()
                self.assertEqual(attempt.decision, "rejected")
                self.assertIn("Валюты", attempt.reason)
                self.assert_no_financial_changes()
                self.share.currency = "usd"
                self.share.save()
                self.portfolio.base_currency = "USD"
                self.portfolio.save()

    def test_invalid_execution_prices_are_refusals_and_cursor_recovers(self):
        for price in ("0", "-1", "0.00000000000000000001"):
            with self.subTest(price=price):
                strategy = self.strategy(direction="<=")
                quote = self.observation(price)
                self.assertEqual(strategy_runner.process_strategy(strategy), 1)
                strategy.refresh_from_db()
                attempt = strategy.attempts.get()
                self.assertEqual(attempt.decision, "rejected")
                self.assertTrue(attempt.reason)
                self.assertEqual(strategy.cursor_id, quote.id)
                self.assertEqual(strategy_runner.process_strategy(strategy), 0)
                self.assertFalse(strategy.attempts.filter(trade__isnull=False))
        self.assert_no_financial_changes()

    def test_base_value_error_from_accounting_becomes_a_refusal(self):
        strategy = self.strategy()
        self.observation("100")
        with patch.object(
            strategy_runner,
            "execute_trade",
            side_effect=ValueError("точность"),
        ):
            self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.assertEqual(strategy.attempts.get().reason, "точность")
        self.assert_no_financial_changes()
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)

    def test_nonfinite_other_source_currency_and_future_do_not_trade(self):
        strategy = self.strategy()
        for invalid in ("NaN", "Infinity", "not-a-number"):
            self.observation(invalid)
        self.observation("100", source="tinkoff")
        self.observation("100", currency="EUR")
        self.observation("100", quote_time=timezone.now() + timedelta(days=1))
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.assertFalse(strategy.attempts.exists())
        self.assert_no_financial_changes()
        self.observation("100", currency="usd")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)

    def test_execution_uses_fresh_time_not_stale_processing_cutoff(self):
        strategy = self.strategy()
        cutoff = timezone.now() - timedelta(minutes=10)
        manual = portfolio_accounting.execute_trade(
            self.portfolio,
            side="BUY",
            share=self.share,
            quantity="1",
            price="1",
            executed_at=timezone.now() - timedelta(minutes=5),
        )
        quote = self.observation("100")
        before_execution = timezone.now()
        self.assertEqual(
            strategy_runner.process_strategy(strategy, now=cutoff), 1
        )
        attempt = strategy.attempts.get()
        self.assertEqual(attempt.decision, "executed")
        self.assertEqual(attempt.quote_time, quote.quote_time)
        self.assertGreater(attempt.trade.executed_at, manual.executed_at)
        self.assertGreaterEqual(attempt.trade.executed_at, before_execution)
        self.assertLessEqual(attempt.trade.executed_at, timezone.now())

    def test_backlogs_of_two_strategies_share_cash_and_current_chronology(
        self,
    ):
        self.strategy(name="First")
        self.strategy(name="Second")
        for price in ("120", "80", "130"):
            self.observation(price)
        cutoff = timezone.now() - timedelta(minutes=10)
        before_execution = timezone.now()
        self.assertEqual(
            strategy_runner.process_share(self.share, now=cutoff), 4
        )
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash, "500")
        trades = list(self.portfolio.trades.order_by("id"))
        self.assertEqual(len(trades), 4)
        self.assertTrue(
            all(trade.executed_at >= before_execution for trade in trades)
        )
        self.assertEqual(
            [trade.executed_at for trade in trades],
            sorted(trade.executed_at for trade in trades),
        )
        self.assertEqual(strategy_runner.process_share(self.share), 0)

    def test_duplicate_attempt_preserves_snapshot_and_never_reexecutes(self):
        strategy = self.strategy()
        quote = self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        attempt = strategy.attempts.get()
        trade_id = attempt.trade_id
        strategy.name = "Renamed"
        strategy.quantity = "2"
        strategy.threshold = "90"
        strategy.state = "armed"
        strategy.cursor_id = 0
        strategy.head_quote_time = None
        strategy.save()
        self.portfolio.name = "Renamed portfolio"
        self.portfolio.save(update_fields=["name"])
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        attempt.refresh_from_db()
        self.portfolio.refresh_from_db()
        self.assertEqual(attempt.strategy_name, "Service strategy")
        self.assertEqual(attempt.portfolio_name, "Service portfolio")
        self.assertEqual(attempt.threshold, "100")
        self.assertEqual(attempt.quantity, "1")
        self.assertEqual(attempt.observed_price, "100")
        self.assertEqual(attempt.quote_time, quote.quote_time)
        self.assertEqual(attempt.trade_id, trade_id)
        self.assertEqual(self.portfolio.cash, "900")
        self.assertEqual(self.portfolio.trades.count(), 1)
        strategy.delete()
        attempt.refresh_from_db()
        self.assertIsNone(attempt.strategy_id)
        self.assertEqual(attempt.trade_id, trade_id)

    def test_baseline_creation_and_reenable_do_not_replay_old_prices(self):
        self.observation("150")
        self.observation("not-a-number")
        strategy = self.strategy()
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.observation("150")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        strategy.enabled = False
        strategy.save()
        self.observation("160")
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        strategy.enabled = True
        strategy.save()
        strategy_runner.reset_baseline(strategy, on_change=True)
        self.observation("170")
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.observation("90")
        self.observation("100")
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.assertEqual(strategy.attempts.count(), 2)

    def test_sql_failure_after_trades_rolls_back_entire_window_then_recovers(
        self,
    ):
        strategy = self.strategy()
        for price in ("120", "80", "130"):
            self.observation(price)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TRIGGER completion_attempt_update_failure
                BEFORE UPDATE ON dashboard_strategyattempt
                WHEN NEW.decision = 'executed'
                  AND NEW.observed_price = '130'
                  AND EXISTS (
                    SELECT 1 FROM dashboard_trade WHERE id = NEW.trade_id
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'final attempt write failed');
                END
                """
            )
        try:
            with self.assertRaises(IntegrityError):
                strategy_runner.process_strategy(strategy)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DROP TRIGGER completion_attempt_update_failure"
                )
        self.assert_rule_unprocessed(strategy)
        self.assert_no_financial_changes()
        self.assertEqual(PriceObservation.objects.count(), 3)
        self.assertEqual(strategy_runner.process_strategy(strategy), 2)
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash, "750")
        self.assertEqual(strategy.attempts.count(), 2)
        self.assertEqual(self.portfolio.trades.count(), 2)

    def test_actual_trade_insert_error_is_not_consumed_as_business_refusal(
        self,
    ):
        strategy = self.strategy()
        self.observation("100")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TRIGGER completion_trade_insert_failure
                BEFORE INSERT ON dashboard_trade
                BEGIN
                    SELECT RAISE(ABORT, 'trade persistence failed');
                END
                """
            )
        try:
            with self.assertRaises(portfolio_accounting.TradeValidationError):
                strategy_runner.process_strategy(strategy)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER completion_trade_insert_failure")
        self.assert_rule_unprocessed(strategy)
        self.assert_no_financial_changes()
        self.assertEqual(strategy_runner.process_strategy(strategy), 1)
        self.assertEqual(strategy_runner.process_strategy(strategy), 0)
        self.assertEqual(strategy.attempts.get().decision, "executed")
        self.assertEqual(Trade.objects.count(), 1)
