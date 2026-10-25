from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from dashboard.forms import PortfolioTransactionForm, ShareModelForm
from dashboard.models import (
    PortfolioPosition,
    PortfolioTransaction,
    PriceObservation,
    Share,
    VirtualPortfolio,
)
from dashboard.services.portfolios import (
    execute_portfolio_transaction,
    value_portfolio,
)


User = get_user_model()
BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_share(**overrides):
    fields = {
        "figi": "PORTFOLIO-FIGI",
        "isin": "PORTFOLIO-ISIN",
        "name": "Portfolio share",
        "ticker": "PORT",
        "currency": "USD",
        "uid": "PORTFOLIO-UID",
    }
    fields.update(overrides)
    return Share.objects.create(**fields)


class PortfolioLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="portfolio@example.com",
            email="portfolio@example.com",
            password="safe-password",
        )
        self.share = make_share()
        self.portfolio = VirtualPortfolio.objects.create(
            owner=self.user,
            name="Ledger",
            currency="USD",
            valuation_source=PriceObservation.Source.DEMO,
            starting_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
        )

    def execute(
        self,
        *,
        side=PortfolioTransaction.Side.BUY,
        quantity="1",
        price="100",
        commission="0",
        currency="usd",
        executed_at=None,
        key=None,
        share=None,
    ):
        return execute_portfolio_transaction(
            portfolio_id=self.portfolio.pk,
            share_id=(share or self.share).pk,
            side=side,
            quantity=Decimal(quantity),
            execution_price=Decimal(price),
            commission=Decimal(commission),
            currency=currency,
            executed_at=executed_at or BASE_TIME,
            idempotency_key=key or uuid4(),
        )

    def add_observation(
        self,
        price,
        *,
        source=PriceObservation.Source.DEMO,
        currency="usd",
        observed_at=BASE_TIME,
    ):
        return PriceObservation.objects.create(
            share=self.share,
            price=Decimal(price),
            currency=currency,
            source=source,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )

    def test_sample_buy_sell_and_portfolio_valuation(self):
        self.execute(
            quantity="2",
            price="100",
            commission="2",
            executed_at=BASE_TIME,
        )
        self.execute(
            quantity="3",
            price="120",
            commission="3",
            executed_at=BASE_TIME + timedelta(minutes=1),
        )
        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="2",
            price="130",
            commission="2",
            executed_at=BASE_TIME + timedelta(minutes=2),
        )

        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.portfolio.refresh_from_db()
        self.assertEqual(position.quantity, Decimal("3.000000000"))
        self.assertEqual(position.average_cost, Decimal("113.000000000"))
        self.assertEqual(position.realized_pnl, Decimal("32.000000000"))
        self.assertEqual(self.portfolio.cash_balance, Decimal("693.000000000"))

        self.add_observation("125", observed_at=BASE_TIME + timedelta(minutes=2))
        valuation = value_portfolio(
            self.portfolio,
            valued_at=BASE_TIME + timedelta(minutes=3),
        )
        self.assertTrue(valuation.is_complete)
        self.assertEqual(valuation.realized_pnl, Decimal("32.000000000"))
        self.assertEqual(valuation.unrealized_pnl, Decimal("36.000000000"))
        self.assertEqual(valuation.total_value, Decimal("1068.000000000"))
        self.assertEqual(valuation.total_result, Decimal("68.000000000"))
        self.assertEqual(valuation.return_percentage, Decimal("6.800000"))
        self.assertEqual(valuation.positions[0].market_observed_at, BASE_TIME + timedelta(minutes=2))

    def test_unrounded_cost_basis_preserves_fees_through_partial_and_full_sales(self):
        self.execute(quantity="3", price="1", commission="0.000000001")
        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.assertEqual(position.average_cost, Decimal("1.000000000"))
        self.assertEqual(position.cost_basis, Decimal("3.000000001"))

        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="1",
            price="1",
            executed_at=BASE_TIME + timedelta(minutes=1),
        )
        position.refresh_from_db()
        self.assertEqual(position.quantity, Decimal("2.000000000"))
        self.assertEqual(position.cost_basis, Decimal("2.000000001"))
        self.assertEqual(position.realized_pnl, Decimal("0"))
        self.add_observation(
            "1",
            observed_at=BASE_TIME + timedelta(minutes=1),
        )
        self.portfolio.refresh_from_db()
        partial_valuation = value_portfolio(
            self.portfolio,
            valued_at=BASE_TIME + timedelta(minutes=1),
        )
        self.assertEqual(
            partial_valuation.realized_pnl + partial_valuation.unrealized_pnl,
            Decimal("-0.000000001"),
        )
        self.assertEqual(
            partial_valuation.total_result,
            Decimal("-0.000000001"),
        )

        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="2",
            price="1",
            executed_at=BASE_TIME + timedelta(minutes=2),
        )
        position.refresh_from_db()
        self.portfolio.refresh_from_db()
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.cost_basis, Decimal("0"))
        self.assertEqual(position.average_cost, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("-0.000000001"))
        self.portfolio.refresh_from_db()
        valuation = value_portfolio(
            self.portfolio,
            valued_at=BASE_TIME + timedelta(minutes=3),
        )
        self.assertEqual(valuation.total_result, Decimal("-0.000000001"))
        self.assertEqual(valuation.realized_pnl, valuation.total_result)

        self.execute(
            quantity="1",
            price="2",
            commission="0.000000001",
            executed_at=BASE_TIME + timedelta(minutes=3),
        )
        position.refresh_from_db()
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertEqual(position.cost_basis, Decimal("2.000000001"))
        self.assertEqual(position.average_cost, Decimal("2.000000001"))

    def test_large_full_close_uses_remaining_basis_instead_of_rounded_average(self):
        self.portfolio.starting_cash = Decimal("2000000000")
        self.portfolio.cash_balance = Decimal("2000000000")
        self.portfolio.save(update_fields=("starting_cash", "cash_balance"))
        self.execute(
            quantity="1000000000",
            price="1",
            commission="0.01",
        )
        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.assertEqual(position.average_cost, Decimal("1.000000000"))
        self.assertEqual(position.cost_basis, Decimal("1000000000.010000000"))

        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="1000000000",
            price="1",
            executed_at=BASE_TIME + timedelta(minutes=1),
        )
        position.refresh_from_db()
        self.portfolio.refresh_from_db()
        self.assertEqual(position.realized_pnl, Decimal("-0.010000000"))
        self.assertEqual(position.cost_basis, Decimal("0"))
        self.assertEqual(self.portfolio.cash_balance, Decimal("1999999999.990000000"))

    def test_sqlite_precision_limit_rejects_unstorable_input_before_writing(self):
        key = uuid4()
        with self.assertRaisesMessage(ValidationError, "15 значащих цифр"):
            self.execute(
                quantity="1000000.000000001",
                price="0.000001",
                key=key,
            )
        self.assertEqual(PortfolioTransaction.objects.count(), 0)
        self.assertFalse(PortfolioPosition.objects.exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash_balance, Decimal("1000"))

        form = PortfolioTransactionForm(
            data={
                "share": str(self.share.pk),
                "side": PortfolioTransaction.Side.BUY,
                "quantity": "1000000.000000001",
                "execution_price": "0.000001",
                "commission": "0",
                "currency": "USD",
                "executed_at": "2026-01-01T12:00",
                "idempotency_key": str(uuid4()),
            },
            portfolio=self.portfolio,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)
        self.assertEqual(PortfolioTransaction.objects.count(), 0)

    def test_unstorable_computed_gross_is_rejected_without_changes(self):
        self.portfolio.starting_cash = Decimal("1000000000000000000")
        self.portfolio.cash_balance = Decimal("1000000000000000000")
        self.portfolio.save(update_fields=("starting_cash", "cash_balance"))

        with self.assertRaisesMessage(ValidationError, "15 значащих цифр"):
            self.execute(
                quantity="999999999999999",
                price="1.000000001",
            )

        self.assertEqual(PortfolioTransaction.objects.count(), 0)
        self.assertFalse(PortfolioPosition.objects.exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(
            self.portfolio.cash_balance,
            Decimal("1000000000000000000"),
        )

    def test_idempotency_is_stable_after_reading_sqlite_decimal_values(self):
        key = uuid4()
        first, created = self.execute(
            key=key,
            quantity="1000000",
            price="0.000001",
        )
        self.assertTrue(created)
        first.refresh_from_db()
        retry, retried = self.execute(
            key=key,
            quantity=str(first.quantity),
            price=str(first.execution_price),
        )
        self.assertFalse(retried)
        self.assertEqual(retry.pk, first.pk)

    def test_full_close_clears_open_basis_and_later_buy_starts_new_basis(self):
        self.execute(quantity="2", price="100", commission="2")
        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="2",
            price="120",
            commission="1",
            executed_at=BASE_TIME + timedelta(minutes=1),
        )
        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.average_cost, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("37.000000000"))

        self.execute(
            quantity="1",
            price="50",
            commission="1",
            executed_at=BASE_TIME + timedelta(minutes=2),
        )
        position.refresh_from_db()
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertEqual(position.average_cost, Decimal("51.000000000"))
        self.assertEqual(position.realized_pnl, Decimal("37.000000000"))

    def test_insufficient_cash_and_oversell_leave_ledger_unchanged(self):
        with self.assertRaisesMessage(ValidationError, "Недостаточно виртуальных"):
            self.execute(quantity="10", price="1000", commission="1")
        self.assertEqual(PortfolioTransaction.objects.count(), 0)
        self.assertEqual(PortfolioPosition.objects.count(), 0)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash_balance, Decimal("1000"))

        self.execute(quantity="1", price="100")
        with self.assertRaisesMessage(ValidationError, "Нельзя продать больше"):
            self.execute(
                side=PortfolioTransaction.Side.SELL,
                quantity="2",
                price="120",
                executed_at=BASE_TIME + timedelta(minutes=1),
            )
        self.assertEqual(PortfolioTransaction.objects.count(), 1)
        self.assertEqual(PortfolioPosition.objects.get().quantity, Decimal("1"))

    def test_invalid_amounts_currency_and_nonchronological_dates_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.execute(quantity="0")
        with self.assertRaisesMessage(ValidationError, "не более 9 знаков"):
            self.execute(quantity="1.0000000001")
        with self.assertRaises(ValidationError):
            self.execute(price="0")
        with self.assertRaises(ValidationError):
            self.execute(commission="-0.01")
        with self.assertRaisesMessage(ValidationError, "Валюта операции"):
            self.execute(currency="RUB")
        rub_share = make_share(
            figi="RUB-FIGI",
            isin="RUB-ISIN",
            ticker="RUBS",
            uid="RUB-UID",
            currency="RUB",
        )
        with self.assertRaisesMessage(ValidationError, "Валюта инструмента"):
            self.execute(share=rub_share)

        self.execute(executed_at=BASE_TIME + timedelta(minutes=5))
        with self.assertRaisesMessage(ValidationError, "раньше последней"):
            self.execute(executed_at=BASE_TIME + timedelta(minutes=4))
        with self.assertRaisesMessage(ValidationError, "не может быть в будущем"):
            self.execute(executed_at=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_equal_timestamps_have_stable_transaction_primary_key_order(self):
        self.execute(quantity="1", price="100", executed_at=BASE_TIME)
        self.execute(quantity="1", price="110", executed_at=BASE_TIME)
        records = list(
            self.portfolio.transactions.filter(executed_at=BASE_TIME).order_by(
                "executed_at", "pk"
            )
        )
        self.assertEqual(len(records), 2)
        self.assertLess(records[0].pk, records[1].pk)

    def test_idempotency_returns_same_operation_and_rejects_changed_payload(self):
        key = uuid4()
        first, created = self.execute(key=key, quantity="2", price="100")
        retry, retried_created = self.execute(key=key, quantity="2", price="100")
        self.assertTrue(created)
        self.assertFalse(retried_created)
        self.assertEqual(first.pk, retry.pk)
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

        with self.assertRaisesMessage(ValidationError, "Ключ повтора"):
            self.execute(key=key, quantity="2", price="101")
        self.assertEqual(PortfolioTransaction.objects.count(), 1)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.cash_balance, Decimal("800.000000000"))

    def test_valuation_uses_only_selected_source_currency_and_not_future_quotes(self):
        self.execute(quantity="1", price="100")
        self.add_observation(
            "140",
            source=PriceObservation.Source.TINKOFF,
            currency="USD",
            observed_at=BASE_TIME,
        )
        self.add_observation(
            "150",
            source=PriceObservation.Source.DEMO,
            currency="RUB",
            observed_at=BASE_TIME,
        )
        self.add_observation(
            "130",
            currency="USD",
            observed_at=BASE_TIME + timedelta(minutes=1),
        )
        valuation = value_portfolio(self.portfolio, valued_at=BASE_TIME)
        self.assertFalse(valuation.is_complete)
        self.assertIsNone(valuation.total_value)
        self.assertIsNone(valuation.total_result)
        self.assertIsNone(valuation.positions[0].market_price)

        valuation = value_portfolio(
            self.portfolio,
            valued_at=BASE_TIME + timedelta(minutes=2),
        )
        self.assertTrue(valuation.is_complete)
        self.assertEqual(valuation.positions[0].market_price, Decimal("130"))

    def test_unknown_quote_makes_open_portfolio_incomplete_but_closed_needs_none(self):
        self.execute(quantity="1", price="100")
        valuation = value_portfolio(self.portfolio, valued_at=BASE_TIME)
        self.assertFalse(valuation.is_complete)
        self.assertIsNone(valuation.total_value)
        self.assertIsNone(valuation.return_percentage)

        self.execute(
            side=PortfolioTransaction.Side.SELL,
            quantity="1",
            price="110",
            executed_at=BASE_TIME + timedelta(minutes=1),
        )
        valuation = value_portfolio(self.portfolio, valued_at=BASE_TIME + timedelta(minutes=2))
        self.assertTrue(valuation.is_complete)
        self.assertEqual(valuation.total_value, self.portfolio.cash_balance)
        self.assertEqual(valuation.unrealized_pnl, Decimal("0"))

    def test_transaction_snapshot_survives_share_deletion(self):
        transaction_record, _ = self.execute(quantity="1", price="100")
        self.share.delete()
        transaction_record.refresh_from_db()
        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.assertIsNone(transaction_record.share_id)
        self.assertIsNone(position.share_id)
        self.assertEqual(transaction_record.share_ticker, "PORT")
        self.assertEqual(transaction_record.share_name, "Portfolio share")
        self.assertEqual(position.share_ticker, "PORT")
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertFalse(value_portfolio(self.portfolio).is_complete)


class PortfolioViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="portfolio-owner@example.com",
            email="portfolio-owner@example.com",
            password="safe-password",
        )
        self.other = User.objects.create_user(
            username="portfolio-other@example.com",
            email="portfolio-other@example.com",
            password="safe-password",
        )
        self.share = make_share(
            figi="PORTFOLIO-VIEW-FIGI",
            isin="PORTFOLIO-VIEW-ISIN",
            ticker="PVW",
            uid="PORTFOLIO-VIEW-UID",
        )
        self.portfolio = VirtualPortfolio.objects.create(
            owner=self.owner,
            name="Private portfolio",
            currency="USD",
            valuation_source=PriceObservation.Source.DEMO,
            starting_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
        )

    def test_create_portfolio_and_virtual_operations_through_http_forms(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:portfolios"),
            {
                "name": "HTTP portfolio",
                "currency": "usd",
                "valuation_source": PriceObservation.Source.DEMO,
                "starting_cash": "1000",
            },
        )
        self.assertEqual(response.status_code, 302)
        portfolio = VirtualPortfolio.objects.get(name="HTTP portfolio")
        self.assertEqual(portfolio.currency, "USD")
        self.assertEqual(portfolio.cash_balance, Decimal("1000"))

        url = reverse(
            "dashboard:portfolio-transaction-create",
            args=(portfolio.pk,),
        )
        form_response = self.client.get(url)
        self.assertEqual(form_response.status_code, 200)
        key = form_response.context["form"]["idempotency_key"].value()
        data = {
            "share": self.share.pk,
            "side": PortfolioTransaction.Side.BUY,
            "quantity": "2",
            "execution_price": "100",
            "commission": "2",
            "currency": "USD",
            "executed_at": "2026-01-01T12:00",
            "idempotency_key": str(key),
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PortfolioTransaction.objects.filter(portfolio=portfolio).count(), 1)
        self.assertFalse(self.share.price_observations.exists())

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PortfolioTransaction.objects.filter(portfolio=portfolio).count(), 1)

        PriceObservation.objects.create(
            share=self.share,
            price=Decimal("125"),
            currency="usd",
            source=PriceObservation.Source.DEMO,
            observed_at=BASE_TIME,
        )
        data.update(
            {
                "side": PortfolioTransaction.Side.SELL,
                "quantity": "1",
                "execution_price": "130",
                "commission": "2",
                "executed_at": "2026-01-01T12:01",
                "idempotency_key": str(uuid4()),
            }
        )
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        detail = self.client.get(
            reverse("dashboard:portfolio-detail", args=(portfolio.pk,))
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "ручная цена виртуальной сделки")
        self.assertContains(detail, "125")
        self.assertContains(detail, "Продажа")
        self.assertContains(detail, "ДЕМО")
        portfolio.refresh_from_db()
        self.assertEqual(portfolio.cash_balance, Decimal("926.000000000"))
        self.assertEqual(PortfolioTransaction.objects.filter(portfolio=portfolio).count(), 2)
        self.assertEqual(self.share.price_observations.count(), 1)

    def test_another_user_cannot_view_or_submit_to_private_portfolio(self):
        self.client.force_login(self.other)
        detail_url = reverse(
            "dashboard:portfolio-detail",
            args=(self.portfolio.pk,),
        )
        operation_url = reverse(
            "dashboard:portfolio-transaction-create",
            args=(self.portfolio.pk,),
        )
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.get(operation_url).status_code, 404)
        self.assertEqual(self.client.post(operation_url, {}).status_code, 404)
        self.assertEqual(PortfolioTransaction.objects.count(), 0)

    def test_portfolio_mutations_require_post_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        create_url = reverse("dashboard:portfolios")
        self.assertEqual(client.post(create_url, {}).status_code, 403)
        operation_url = reverse(
            "dashboard:portfolio-transaction-create",
            args=(self.portfolio.pk,),
        )
        self.assertEqual(
            client.get(operation_url).status_code,
            200,
        )
        self.assertEqual(client.post(operation_url, {}).status_code, 403)

    def test_html_share_delete_keeps_transaction_and_position_snapshots(self):
        transaction_record, _ = execute_portfolio_transaction(
            portfolio_id=self.portfolio.pk,
            share_id=self.share.pk,
            side=PortfolioTransaction.Side.BUY,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            commission=Decimal("1"),
            currency="USD",
            executed_at=BASE_TIME,
            idempotency_key=uuid4(),
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:delete", args=(self.share.pk,))
        )

        self.assertEqual(response.status_code, 302)
        transaction_record.refresh_from_db()
        position = PortfolioPosition.objects.get(portfolio=self.portfolio)
        self.assertIsNone(transaction_record.share_id)
        self.assertEqual(transaction_record.share_ticker, "PVW")
        self.assertEqual(transaction_record.share_name, "Portfolio share")
        self.assertIsNone(position.share_id)
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertEqual(
            self.portfolio.transactions.filter(pk=transaction_record.pk).count(),
            1,
        )

    def test_api_share_delete_keeps_transaction_snapshot(self):
        transaction_record, _ = execute_portfolio_transaction(
            portfolio_id=self.portfolio.pk,
            share_id=self.share.pk,
            side=PortfolioTransaction.Side.BUY,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            commission=Decimal("0"),
            currency="USD",
            executed_at=BASE_TIME,
            idempotency_key=uuid4(),
        )
        client = APIClient()
        client.force_authenticate(self.owner)

        response = client.delete(f"/api/v1/shares/{self.share.pk}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        transaction_record.refresh_from_db()
        self.assertIsNone(transaction_record.share_id)
        self.assertEqual(transaction_record.share_ticker, "PVW")
        self.assertEqual(transaction_record.share_name, "Portfolio share")
        self.assertEqual(PortfolioTransaction.objects.count(), 1)

    def test_identity_change_is_blocked_after_virtual_trade_but_rename_is_allowed(self):
        self.execute_portfolio_transaction_for_identity_test()
        client = APIClient()
        client.force_authenticate(self.owner)

        rename_response = client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"name": "Renamed share"},
            format="json",
        )
        self.assertEqual(rename_response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Renamed share")
        self.assertEqual(
            PortfolioPosition.objects.get(portfolio=self.portfolio).quantity,
            Decimal("1.000000000"),
        )

        currency_response = client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"currency": "RUB"},
            format="json",
        )
        self.assertEqual(
            currency_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.share.refresh_from_db()
        self.assertEqual(self.share.currency, "USD")

        response = client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"figi": "REPLACEMENT-FIGI", "uid": "REPLACEMENT-UID", "ticker": "NEW"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Создайте новый инструмент", str(response.data))
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, "PORTFOLIO-VIEW-FIGI")
        self.assertEqual(self.share.uid, "PORTFOLIO-VIEW-UID")
        self.assertEqual(self.share.ticker, "PVW")

        self.client.force_login(self.owner)
        html_form = ShareModelForm(instance=self.share)
        html_data = {
            field_name: html_form[field_name].value() or ""
            for field_name in html_form.fields
        }
        html_data.update(
            {"figi": "HTML-REPLACEMENT", "uid": "HTML-REPLACEMENT-UID", "ticker": "HTML"}
        )
        html_response = self.client.post(
            reverse("dashboard:update", args=(self.share.pk,)),
            html_data,
        )
        self.assertEqual(html_response.status_code, 200)
        self.assertContains(html_response, "Создайте новый инструмент")

        admin_user = User.objects.create_superuser(
            username="portfolio-identity-admin@example.com",
            email="portfolio-identity-admin@example.com",
            password="unused-test-password",
        )
        self.client.force_login(admin_user)
        admin_data = dict(html_data)
        admin_data.update(
            {"figi": "ADMIN-REPLACEMENT", "uid": "ADMIN-REPLACEMENT-UID", "_save": "Save"}
        )
        admin_response = self.client.post(
            reverse("admin:dashboard_share_change", args=(self.share.pk,)),
            admin_data,
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "Создайте новый инструмент")
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, "PORTFOLIO-VIEW-FIGI")

    def execute_portfolio_transaction_for_identity_test(self):
        return execute_portfolio_transaction(
            portfolio_id=self.portfolio.pk,
            share_id=self.share.pk,
            side=PortfolioTransaction.Side.BUY,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            commission=Decimal("0"),
            currency="USD",
            executed_at=BASE_TIME,
            idempotency_key=uuid4(),
        )
