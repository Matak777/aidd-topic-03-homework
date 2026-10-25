from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PortfolioProjectionReplayMigrationTests(TransactionTestCase):
    migrate_from = ("dashboard", "0015_portfolioposition_portfolio_position_cost_basis_nonnegative")
    migrate_to = ("dashboard", "0016_replay_portfolio_projections")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        executor = MigrationExecutor(connection)
        executor.migrate([cls.migrate_from])
        cls.old_apps = executor.loader.project_state([cls.migrate_from]).apps

    @classmethod
    def tearDownClass(cls):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDownClass()

    def test_replay_repairs_existing_projections_without_changing_ledger_or_cash(self):
        Portfolio = self.old_apps.get_model("dashboard", "VirtualPortfolio")
        Position = self.old_apps.get_model("dashboard", "PortfolioPosition")
        Share = self.old_apps.get_model("dashboard", "Share")
        Transaction = self.old_apps.get_model("dashboard", "PortfolioTransaction")
        user = get_user_model().objects.create_user(
            username="projection-replay@example.invalid",
            email="projection-replay@example.invalid",
            password="migration-test-password",
        )
        share = Share.objects.create(
            figi="REPLAY-FIGI",
            isin="REPLAY-ISIN",
            name="Replay instrument",
            ticker="RPLY",
            currency="USD",
            uid="REPLAY-UID",
        )
        event_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def portfolio(name, cash="1000"):
            return Portfolio.objects.create(
                owner_id=user.pk,
                name=name,
                currency="USD",
                valuation_source="demo",
                starting_cash=Decimal(cash),
                cash_balance=Decimal(cash),
            )

        def transaction_record(portfolio_obj, side, quantity, price, fee, minute):
            return Transaction.objects.create(
                portfolio_id=portfolio_obj.pk,
                share_id=share.pk,
                share_ticker="RPLY",
                share_name="Replay instrument",
                side=side,
                quantity=Decimal(quantity),
                execution_price=Decimal(price),
                commission=Decimal(fee),
                currency="USD",
                executed_at=event_time.replace(minute=minute),
                idempotency_key=uuid4(),
            )

        close_portfolio = portfolio("nano fee full close")
        transaction_record(close_portfolio, "buy", "3", "1", "0.000000001", 1)
        transaction_record(close_portfolio, "sell", "3", "1", "0", 2)
        close_position = Position.objects.create(
            portfolio_id=close_portfolio.pk,
            share_id=share.pk,
            share_ticker="RPLY",
            share_name="Replay instrument",
            quantity=Decimal("0"),
            average_cost=Decimal("0"),
            realized_pnl=Decimal("0"),
        )
        close_cash = close_portfolio.cash_balance = Decimal("999.999999999")
        close_portfolio.save(update_fields=("cash_balance",))

        partial_portfolio = portfolio("partial sale")
        transaction_record(partial_portfolio, "buy", "3", "1", "0.000000001", 3)
        transaction_record(partial_portfolio, "sell", "1", "1", "0", 4)
        partial_position = Position.objects.create(
            portfolio_id=partial_portfolio.pk,
            share_id=share.pk,
            share_ticker="RPLY",
            share_name="Replay instrument",
            quantity=Decimal("2"),
            average_cost=Decimal("1"),
            realized_pnl=Decimal("0"),
        )
        partial_portfolio.cash_balance = Decimal("997.999999999")
        partial_portfolio.save(update_fields=("cash_balance",))

        rounded_portfolio = portfolio("per-operation half even")
        transaction_record(rounded_portfolio, "buy", "0.1", "0.000000005", "0", 5)
        transaction_record(rounded_portfolio, "buy", "0.1", "0.000000005", "0", 6)
        rounded_position = Position.objects.create(
            portfolio_id=rounded_portfolio.pk,
            share_id=share.pk,
            share_ticker="RPLY",
            share_name="Replay instrument",
            quantity=Decimal("0.2"),
            average_cost=Decimal("0"),
            realized_pnl=Decimal("0"),
        )

        ledger_before = list(
            Transaction.objects.order_by("pk").values_list(
                "pk",
                "portfolio_id",
                "share_id",
                "share_ticker",
                "share_name",
                "side",
                "quantity",
                "execution_price",
                "commission",
                "currency",
                "executed_at",
                "idempotency_key",
            )
        )
        cash_before = dict(
            Portfolio.objects.values_list("pk", "cash_balance")
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        NewPosition = new_apps.get_model("dashboard", "PortfolioPosition")
        NewPortfolio = new_apps.get_model("dashboard", "VirtualPortfolio")
        NewTransaction = new_apps.get_model("dashboard", "PortfolioTransaction")

        close_position = NewPosition.objects.get(pk=close_position.pk)
        self.assertEqual(close_position.quantity, Decimal("0E-9"))
        self.assertEqual(close_position.cost_basis, Decimal("0E-9"))
        self.assertEqual(close_position.average_cost, Decimal("0E-9"))
        self.assertEqual(close_position.realized_pnl, Decimal("-0.000000001"))

        partial_position = NewPosition.objects.get(pk=partial_position.pk)
        self.assertEqual(partial_position.quantity, Decimal("2.000000000"))
        self.assertEqual(partial_position.cost_basis, Decimal("2.000000001"))
        self.assertEqual(partial_position.average_cost, Decimal("1.000000000"))
        self.assertEqual(partial_position.realized_pnl, Decimal("0E-9"))

        rounded_position = NewPosition.objects.get(pk=rounded_position.pk)
        self.assertEqual(rounded_position.quantity, Decimal("0.200000000"))
        self.assertEqual(rounded_position.cost_basis, Decimal("0E-9"))
        self.assertEqual(rounded_position.average_cost, Decimal("0E-9"))

        cash_after = dict(NewPortfolio.objects.values_list("pk", "cash_balance"))
        self.assertEqual(cash_after, cash_before)
        self.assertEqual(cash_after[close_portfolio.pk], Decimal("999.999999999"))
        self.assertEqual(cash_after[partial_portfolio.pk], Decimal("997.999999999"))
        self.assertEqual(cash_after[rounded_portfolio.pk], Decimal("1000.000000000"))
        self.assertEqual(
            list(
                NewTransaction.objects.order_by("pk").values_list(
                    "pk",
                    "portfolio_id",
                    "share_id",
                    "share_ticker",
                    "share_name",
                    "side",
                    "quantity",
                    "execution_price",
                    "commission",
                    "currency",
                    "executed_at",
                    "idempotency_key",
                )
            ),
            ledger_before,
        )
        self.assertEqual(close_cash, Decimal("999.999999999"))
