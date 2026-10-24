from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from dashboard.models import Share
from dashboard.services.market_data import MarketDataError, get_last_data

User = get_user_model()


def make_share(**overrides):
    fields = {
        "figi": "BBG000B9XRY4",
        "isin": "US0378331005",
        "name": "Apple",
        "ticker": "AAPL",
        "currency": "usd",
        "uid": "a9eb4238-eba9-488c-b102-b6140fd08e38",
        "last_price": 100.5,
        "is_trend_high": True,
    }
    fields.update(overrides)
    return Share.objects.create(**fields)


class ShareAPITests(APITestCase):
    def setUp(self):
        self.share = make_share()
        self.user = User.objects.create_user(
            username="api@example.com",
            email="api@example.com",
            password="ValidApiPassword2026!",
        )
        self.client.force_authenticate(self.user)
        self.payload = {
            "figi": "BBG000B9XRY5",
            "isin": "US0378331004",
            "name": "Example Company",
            "ticker": "EXAMPLE",
            "currency": "usd",
            "uid": "a9eb4238-eba9-488c-b102-b6140fd08e39",
            "last_price": 1.5,
            "is_trend_high": False,
        }

    def test_list_and_detail(self):
        response = self.client.get("/api/v1/shares/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["uid"], self.share.uid)

        response = self.client.get(f"/api/v1/shares/{self.share.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["figi"], self.share.figi)

    def test_missing_detail_returns_404(self):
        response = self.client.get("/api/v1/shares/999/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_requires_all_required_model_fields(self):
        response = self.client.post("/api/v1/shares/", self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["uid"], self.payload["uid"])

        response = self.client.post(
            "/api/v1/shares/", {"figi": "BBG000B9XRY6"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_and_delete(self):
        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"name": "Apple Inc."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Apple Inc.")

        response = self.client.delete(f"/api/v1/shares/{self.share.pk}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Share.objects.filter(pk=self.share.pk).exists())

    def test_api_is_readable_anonymously_but_writes_require_authentication(self):
        anonymous_client = APIClient()
        response = anonymous_client.get("/api/v1/shares/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = anonymous_client.post(
            "/api/v1/shares/", self.payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filter_search_and_ordering(self):
        make_share(
            figi="BBG000B9XRY5",
            isin="US0378331004",
            name="Banana Holdings",
            ticker="BNNA",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e39",
        )
        response = self.client.get("/api/v1/shares/?name=Apple")
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["ticker"], "AAPL")

        response = self.client.get("/api/v1/shares/?search=apple")
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["ticker"], "AAPL")

        response = self.client.get("/api/v1/shares/?ordering=-ticker")
        self.assertEqual(
            [share["ticker"] for share in response.data["results"]],
            ["BNNA", "AAPL"],
        )


class ShareWebTests(TestCase):
    def setUp(self):
        self.share = make_share()
        self.user = User.objects.create_user(
            username="web@example.com",
            email="web@example.com",
            password="ValidWebPassword2026!",
        )

    def test_list_and_detail_render(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AAPL")
        self.assertContains(response, "100.5 usd")

        with patch("dashboard.views.get_graph", return_value="<div>chart</div>"):
            response = self.client.get(
                reverse("dashboard:detail", args=(self.share.pk,))
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "chart")

    def test_detail_handles_missing_share_and_provider_error(self):
        response = self.client.get(reverse("dashboard:detail", args=(999,)))
        self.assertEqual(response.status_code, 404)

        with patch(
            "dashboard.views.get_graph",
            side_effect=MarketDataError("Tinkoff API недоступен."),
        ):
            response = self.client.get(
                reverse("dashboard:detail", args=(self.share.pk,))
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tinkoff API недоступен.")

    def test_web_crud_requires_login_and_persists_changes(self):
        create_url = reverse("dashboard:create")
        response = self.client.get(create_url)
        self.assertRedirects(response, reverse("dashboard:index"))

        self.client.force_login(self.user)
        payload = {
            "figi": "BBG000B9XRY5",
            "isin": "US0378331004",
            "name": "Example Company",
            "ticker": "EXAMPLE",
            "currency": "usd",
            "uid": "a9eb4238-eba9-488c-b102-b6140fd08e39",
            "last_price": "1.5",
            "is_trend_high": "on",
        }
        response = self.client.post(create_url, payload)
        self.assertRedirects(response, reverse("dashboard:index"))
        created = Share.objects.get(ticker="EXAMPLE")

        update_url = reverse("dashboard:update", args=(created.pk,))
        payload["name"] = "Updated Company"
        response = self.client.post(update_url, payload)
        self.assertRedirects(response, reverse("dashboard:index"))
        created.refresh_from_db()
        self.assertEqual(created.name, "Updated Company")

        delete_url = reverse("dashboard:delete", args=(created.pk,))
        self.assertEqual(self.client.get(delete_url).status_code, 200)
        response = self.client.post(delete_url)
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertFalse(Share.objects.filter(pk=created.pk).exists())

    def test_invalid_crud_form_renders_validation_errors(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("dashboard:create"),
            {"figi": self.share.figi, "ticker": self.share.ticker},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required", status_code=200)


class UserFlowTests(TestCase):
    def setUp(self):
        self.registration_url = reverse("user:registration")
        self.login_url = reverse("user:auth")
        self.logout_url = reverse("user:logout")
        self.password = "StrongPassword2026!"

    def test_user_routes_include_expected_prefix(self):
        self.assertEqual(self.login_url, "/user/login/")
        self.assertEqual(self.registration_url, "/user/registration/")
        self.assertEqual(self.logout_url, "/user/logout/")

    def test_registration_normalizes_email_and_logs_user_in(self):
        response = self.client.post(
            self.registration_url,
            {
                "email": "  New.User@Example.com ",
                "password1": self.password,
                "password2": self.password,
            },
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        user = User.objects.get(username="new.user@example.com")
        self.assertEqual(user.email, "new.user@example.com")
        self.assertTrue(user.check_password(self.password))
        self.assertIn("_auth_user_id", self.client.session)

    def test_registration_preserves_password_whitespace(self):
        password = "  RegistrationPassword2026!  "
        response = self.client.post(
            self.registration_url,
            {
                "email": "spaced.password@example.com",
                "password1": password,
                "password2": password,
            },
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        user = User.objects.get(username="spaced.password@example.com")
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.check_password(password.strip()))

    def test_registration_distinguishes_passwords_that_only_differ_in_spaces(self):
        response = self.client.post(
            self.registration_url,
            {
                "email": "different.spaces@example.com",
                "password1": "  RegistrationPassword2026!  ",
                "password2": "RegistrationPassword2026!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Введенные пароли не совпадают")
        self.assertFalse(
            User.objects.filter(username="different.spaces@example.com").exists()
        )

    def test_registration_validation_and_case_insensitive_duplicate(self):
        response = self.client.post(
            self.registration_url,
            {"email": "bad", "password1": "short", "password2": "different"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email address")

        User.objects.create_user(
            username="existing@example.com",
            email="existing@example.com",
            password=self.password,
        )
        response = self.client.post(
            self.registration_url,
            {
                "email": "EXISTING@example.com",
                "password1": self.password,
                "password2": self.password,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "уже зарегистрирован")

    def test_login_validates_input_and_normalizes_email(self):
        User.objects.create_user(
            username="login@example.com",
            email="login@example.com",
            password=self.password,
        )
        response = self.client.post(
            self.login_url,
            {"email": "LOGIN@example.com", "password": self.password},
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertIn("_auth_user_id", self.client.session)

        self.client.logout()
        response = self.client.post(
            self.login_url, {"email": "not-an-email", "password": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email address")

        response = self.client.post(
            self.login_url,
            {"email": "login@example.com", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Неверный логин или пароль")

    def test_login_preserves_password_whitespace(self):
        password = "  LoginPassword2026!  "
        User.objects.create_user(
            username="spaced.login@example.com",
            email="spaced.login@example.com",
            password=password,
        )

        response = self.client.post(
            self.login_url,
            {"email": "SPACED.LOGIN@example.com", "password": password},
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertIn("_auth_user_id", self.client.session)

        self.client.logout()
        response = self.client.post(
            self.login_url,
            {"email": "spaced.login@example.com", "password": password.strip()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Неверный логин или пароль")

    def test_logout_is_csrf_protected_post(self):
        self.client.force_login(
            User.objects.create_user(
                username="logout@example.com",
                email="logout@example.com",
                password=self.password,
            )
        )
        self.assertEqual(self.client.get(self.logout_url).status_code, 405)
        response = self.client.post(self.logout_url)
        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertNotIn("_auth_user_id", self.client.session)


class MarketDataTests(TestCase):
    @override_settings(TINKOFF_TOKEN="")
    def test_missing_tinkoff_token_is_explicit(self):
        with self.assertRaisesMessage(MarketDataError, "TINKOFF_TOKEN"):
            from dashboard.services.market_data import get_client

            get_client()

        with self.assertRaisesMessage(CommandError, "TINKOFF_TOKEN"):
            call_command("add_shares")

    @override_settings(
        TINKOFF_TOKEN="test-token",
        TINKOFF_RPC_TIMEOUT_SECONDS=4.5,
    )
    def test_rpc_deadline_is_applied_at_the_mock_transport(self):
        from dashboard.services.market_data import get_client

        with patch("tinkoff.invest.Client") as client_factory:
            get_client()

        interceptor = client_factory.call_args.kwargs["interceptors"][0]
        transport = MagicMock(return_value="response")
        details = SimpleNamespace(
            method="/tinkoff.invest.v1.MarketDataService/GetLastPrices",
            timeout=None,
            metadata=None,
            credentials=None,
            wait_for_ready=None,
            compression=None,
        )
        result = interceptor.intercept_unary_unary(
            transport, details, request="serialized-request"
        )
        self.assertEqual(result, "response")
        self.assertEqual(transport.call_args.args[0].timeout, 4.5)
        self.assertEqual(transport.call_args.args[1], "serialized-request")

        details.timeout = 1.0
        interceptor.intercept_unary_unary(transport, details, request="request")
        self.assertEqual(transport.call_args.args[0].timeout, 1.0)

        details.timeout = float("nan")
        interceptor.intercept_unary_unary(transport, details, request="request")
        self.assertEqual(transport.call_args.args[0].timeout, 4.5)

    def test_tinkoff_sdk_imports_and_serializes_protobuf_requests(self):
        from tinkoff.invest._grpc_helpers import dataclass_to_protobuff
        from tinkoff.invest.grpc.marketdata_pb2 import (
            GetLastPricesRequest as ProtobufGetLastPricesRequest,
        )
        from tinkoff.invest.schemas import GetLastPricesRequest

        request = GetLastPricesRequest(figi=["BBG000B9XRY4"])
        protobuf_request = dataclass_to_protobuff(
            request, ProtobufGetLastPricesRequest()
        )
        serialized = protobuf_request.SerializeToString()
        parsed = ProtobufGetLastPricesRequest.FromString(serialized)
        self.assertEqual(list(parsed.figi), ["BBG000B9XRY4"])

    @override_settings(
        TINKOFF_TOKEN="test-token",
        TINKOFF_RPC_TIMEOUT_SECONDS=3,
    )
    def test_deadline_exceeded_is_reported_for_price_candles_and_catalog(self):
        from grpc import StatusCode
        from tinkoff.invest.exceptions import RequestError

        deadline_error = RequestError(StatusCode.DEADLINE_EXCEEDED, "deadline", None)
        share = make_share()
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.market_data.get_last_prices.side_effect = deadline_error
        with patch(
            "dashboard.services.market_data.get_client", return_value=client_context
        ):
            with self.assertRaisesRegex(MarketDataError, "3 сек."):
                get_last_data(share)

        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.get_all_candles.side_effect = deadline_error
        with patch(
            "dashboard.services.graph.get_client", return_value=client_context
        ):
            from dashboard.services.graph import get_graph

            with self.assertRaisesRegex(MarketDataError, "3 сек."):
                get_graph(share)

        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.side_effect = deadline_error
        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            with self.assertRaisesRegex(CommandError, "3 сек."):
                call_command("add_shares")

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_last_price_includes_fractional_nanos(self):
        share = make_share()
        now = datetime.now(timezone.utc)
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.market_data.get_last_prices.return_value = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    price=SimpleNamespace(units=12, nano=345_000_000),
                    time=now,
                )
            ]
        )
        with patch(
            "dashboard.services.market_data.get_client", return_value=client_context
        ):
            price, timestamp = get_last_data(share)

        self.assertEqual(price, 12.345)
        self.assertEqual(timestamp, now)

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_import_updates_existing_share_by_figi(self):
        from dashboard.management.commands.add_shares import Command

        existing = make_share()
        remote_share = SimpleNamespace(
            figi=existing.figi,
            isin="US0378331006",
            name="Apple Inc.",
            ticker="APPLX",
            currency="usd",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e39",
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            Command().handle()

        existing.refresh_from_db()
        self.assertEqual(Share.objects.count(), 1)
        self.assertEqual(existing.name, "Apple Inc.")
        self.assertEqual(existing.ticker, "APPLX")

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_graph_is_built_from_mocked_daily_candles(self):
        from dashboard.services.graph import get_graph

        share = make_share()
        candle = SimpleNamespace(
            time=datetime.now(timezone.utc),
            close=SimpleNamespace(units=42, nano=500_000_000),
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.get_all_candles.return_value = [candle]

        with (
            patch(
                "dashboard.services.graph.get_client", return_value=client_context
            ),
            patch("dashboard.services.graph.plot", return_value="<div>graph</div>") as plot,
        ):
            result = get_graph(share)

        self.assertEqual(result, "<div>graph</div>")
        self.assertEqual(list(plot.call_args.args[0][0].y), [42.5])
        self.assertEqual(plot.call_args.kwargs["include_plotlyjs"], "cdn")

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_price_task_updates_trend_and_timestamp(self):
        from dashboard.tasks import update_last_price

        share = make_share(last_price=100.0)
        old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
        Share.objects.filter(pk=share.pk).update(updated_at=old_timestamp)

        with patch(
            "dashboard.tasks.get_last_data", return_value=(100.0, datetime.now(timezone.utc))
        ):
            update_last_price.run()

        share.refresh_from_db()
        self.assertEqual(share.last_price, 100.0)
        self.assertFalse(share.is_trend_high)
        self.assertGreater(share.updated_at, old_timestamp)

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_first_price_has_unknown_trend(self):
        from dashboard.tasks import update_last_price

        share = make_share(last_price=None, is_trend_high=None)
        with patch("dashboard.tasks.get_last_data", return_value=(100.0, None)):
            update_last_price.run()
        share.refresh_from_db()
        self.assertEqual(share.last_price, 100.0)
        self.assertIsNone(share.is_trend_high)
