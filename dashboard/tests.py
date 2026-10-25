from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.test import TestCase, override_settings
from django.utils import timezone as django_timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from dashboard.models import (
    PriceAlert,
    PriceObservation,
    PortfolioTransaction,
    Share,
    Watchlist,
    WatchlistItem,
    WatchRule,
    VirtualPortfolio,
)
from dashboard.services.market_data import MarketDataError, get_last_data
from dashboard.services.price_history import get_price_history
from dashboard.services.portfolios import execute_portfolio_transaction

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
        self.assertIn("last_price_source", response.data)
        self.assertEqual(response.data["last_price_source"], "unknown")

    def test_missing_detail_returns_404(self):
        response = self.client.get("/api/v1/shares/999/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_requires_all_required_model_fields(self):
        response = self.client.post("/api/v1/shares/", self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["uid"], self.payload["uid"])
        self.assertEqual(
            response.data["last_price_source"], Share.PriceSource.MANUAL
        )

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

    def test_delete_share_keeps_all_owners_alert_snapshots(self):
        other_user = User.objects.create_user(
            username="other-api@example.com",
            email="other-api@example.com",
            password="safe-password",
        )
        observation = PriceObservation.objects.create(
            share=self.share,
            price=Decimal("101.25"),
            currency="USD",
            source=PriceObservation.Source.DEMO,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        alerts = []
        for user, name in ((self.user, "API owner"), (other_user, "Other owner")):
            watchlist = Watchlist.objects.create(owner=user, name=name)
            item = WatchlistItem.objects.create(
                watchlist=watchlist,
                share=self.share,
            )
            rule = WatchRule.objects.create(
                item=item,
                source=PriceObservation.Source.DEMO,
                currency="usd",
                direction=WatchRule.Direction.AT_LEAST,
                threshold=Decimal("100"),
            )
            alerts.append(
                PriceAlert.objects.create(
                    owner=user,
                    rule=rule,
                    observation=observation,
                    share_ticker=self.share.ticker,
                    share_name=self.share.name,
                    price=observation.price,
                    threshold=Decimal("100"),
                    direction=WatchRule.Direction.AT_LEAST,
                    source=PriceObservation.Source.DEMO,
                    currency=observation.currency,
                    observed_at=observation.observed_at,
                )
            )

        response = self.client.delete(f"/api/v1/shares/{self.share.pk}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Share.objects.filter(pk=self.share.pk).exists())
        self.assertFalse(PriceObservation.objects.filter(pk=observation.pk).exists())
        self.assertEqual(PriceAlert.objects.count(), 2)
        for alert in alerts:
            alert.refresh_from_db()
            self.assertIsNone(alert.observation_id)
            self.assertIsNone(alert.rule_id)
            self.assertEqual(alert.share_ticker, "AAPL")
            self.assertEqual(alert.share_name, "Apple")
            self.assertEqual(alert.price, Decimal("101.25"))

    def test_changing_currency_invalidates_cached_price_but_keeps_observation_currency(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.last_price = 100
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.save()
        PriceObservation.objects.create(
            share=self.share,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )

        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"currency": "rub"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        observation = self.share.price_observations.get()
        self.assertEqual(self.share.currency, "rub")
        self.assertIsNone(self.share.last_price)
        self.assertIsNone(self.share.last_price_source)
        self.assertIsNone(self.share.last_price_observed_at)
        self.assertIsNone(self.share.is_trend_high)
        self.assertEqual(observation.currency, "usd")
        self.assertEqual(observation.price, Decimal("100"))

    def test_changing_figi_without_price_invalidates_cache(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.save()

        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"figi": "BBG000B9XRY9"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, "BBG000B9XRY9")
        self.assertIsNone(self.share.last_price)
        self.assertIsNone(self.share.last_price_source)
        self.assertIsNone(self.share.last_price_observed_at)
        self.assertIsNone(self.share.is_trend_high)

    def test_name_only_patch_preserves_live_price_provenance(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.is_trend_high = True
        self.share.save()

        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"name": "Apple renamed"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Apple renamed")
        self.assertEqual(self.share.last_price, 100.5)
        self.assertEqual(self.share.last_price_source, Share.PriceSource.TINKOFF)
        self.assertEqual(self.share.last_price_observed_at, observed_at)
        self.assertTrue(self.share.is_trend_high)

    def test_identity_change_with_history_is_rejected_without_rewriting_history(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        observation = PriceObservation.objects.create(
            share=self.share,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )
        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"figi": "BBG000B9XRY9"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("figi", response.data)
        self.share.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual(self.share.figi, "BBG000B9XRY4")
        self.assertEqual(observation.price, Decimal("100"))
        self.assertEqual(observation.currency, "usd")

    def test_new_manual_price_during_currency_change_has_manual_provenance(self):
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.save()

        response = self.client.patch(
            f"/api/v1/shares/{self.share.pk}/",
            {"currency": "rub", "last_price": 120},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.share.refresh_from_db()
        self.assertEqual(self.share.last_price, 120)
        self.assertEqual(self.share.last_price_source, Share.PriceSource.MANUAL)
        self.assertIsNone(self.share.last_price_observed_at)
        self.assertIsNone(self.share.is_trend_high)

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

    def test_trend_colors_distinguish_true_false_and_unknown(self):
        make_share(
            figi="BBG000B9XRY5",
            isin="US0378331004",
            name="First live",
            ticker="FIRST",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e39",
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_trend_high=None,
        )
        make_share(
            figi="BBG000B9XRY6",
            isin="US0378331003",
            name="Growing",
            ticker="GROW",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e40",
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_trend_high=True,
        )
        make_share(
            figi="BBG000B9XRY7",
            isin="US0378331002",
            name="Not growing",
            ticker="NOGRO",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e41",
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_trend_high=False,
        )

        response = self.client.get(reverse("dashboard:index"))
        content = response.content.decode()
        self.assertEqual(content.count("mediumseagreen"), 1)
        self.assertEqual(content.count("palevioletred"), 1)
        self.assertIn("FIRST", content)
        self.assertIn("GROW", content)
        self.assertIn("NOGRO", content)

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
        self.assertEqual(created.last_price_source, Share.PriceSource.MANUAL)
        self.assertIsNone(created.last_price_observed_at)

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

    def test_name_change_preserves_live_price_provenance(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.is_trend_high = True
        self.share.save()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("dashboard:update", args=(self.share.pk,)),
            {
                "figi": self.share.figi,
                "isin": self.share.isin,
                "name": "Apple Updated",
                "ticker": self.share.ticker,
                "currency": self.share.currency,
                "uid": self.share.uid,
                "last_price": str(self.share.last_price),
                "is_trend_high": "on",
            },
        )

        self.assertRedirects(response, reverse("dashboard:index"))
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Apple Updated")
        self.assertEqual(self.share.last_price_source, Share.PriceSource.TINKOFF)
        self.assertEqual(self.share.last_price_observed_at, observed_at)

    def test_html_currency_change_invalidates_price_and_identity_change_is_rejected_with_history(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        PriceObservation.objects.create(
            share=self.share,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.save()
        self.client.force_login(self.user)
        base = {
            "figi": self.share.figi,
            "isin": self.share.isin,
            "name": self.share.name,
            "ticker": self.share.ticker,
            "currency": "rub",
            "uid": self.share.uid,
            "last_price": str(self.share.last_price),
        }

        response = self.client.post(
            reverse("dashboard:update", args=(self.share.pk,)),
            base,
        )
        self.assertRedirects(response, reverse("dashboard:index"))
        self.share.refresh_from_db()
        self.assertEqual(self.share.currency, "rub")
        self.assertIsNone(self.share.last_price)
        self.assertIsNone(self.share.last_price_source)
        self.assertIsNone(self.share.last_price_observed_at)
        self.assertEqual(
            self.share.price_observations.get().currency,
            "usd",
        )

        base["figi"] = "BBG000B9XRY9"
        response = self.client.post(
            reverse("dashboard:update", args=(self.share.pk,)),
            base,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Создайте новый инструмент")
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, "BBG000B9XRY4")

    def test_admin_currency_change_invalidates_cached_price(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.share.last_price_source = Share.PriceSource.TINKOFF
        self.share.last_price_observed_at = observed_at
        self.share.save()
        admin_user = User.objects.create_superuser(
            username="admin@example.com",
            email="admin@example.com",
            password="AdminPassword2026!",
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse("admin:dashboard_share_change", args=(self.share.pk,)),
            {
                "figi": self.share.figi,
                "isin": self.share.isin,
                "name": self.share.name,
                "ticker": self.share.ticker,
                "currency": "rub",
                "uid": self.share.uid,
                "last_price": str(self.share.last_price),
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.share.refresh_from_db()
        self.assertEqual(self.share.currency, "rub")
        self.assertIsNone(self.share.last_price)
        self.assertIsNone(self.share.last_price_source)
        self.assertIsNone(self.share.last_price_observed_at)

    def test_admin_identity_change_with_history_is_rejected(self):
        observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        PriceObservation.objects.create(
            share=self.share,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )
        admin_user = User.objects.create_superuser(
            username="identity-admin@example.com",
            email="identity-admin@example.com",
            password="AdminPassword2026!",
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse("admin:dashboard_share_change", args=(self.share.pk,)),
            {
                "figi": "BBG000B9XRY9",
                "isin": self.share.isin,
                "name": self.share.name,
                "ticker": self.share.ticker,
                "currency": self.share.currency,
                "uid": self.share.uid,
                "last_price": str(self.share.last_price),
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Создайте новый инструмент")
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, "BBG000B9XRY4")
        self.assertEqual(self.share.price_observations.count(), 1)

    def test_price_observation_admin_rejects_add_without_server_error(self):
        admin_user = User.objects.create_superuser(
            username="history-admin@example.com",
            email="history-admin@example.com",
            password="AdminPassword2026!",
        )
        self.client.force_login(admin_user)
        add_url = reverse("admin:dashboard_priceobservation_add")

        get_response = self.client.get(add_url)
        post_response = self.client.post(add_url, {})

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(PriceObservation.objects.count(), 0)

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

    def test_registration_rejects_password_equal_to_normalized_email(self):
        response = self.client.post(
            self.registration_url,
            {
                "email": "Same.Password@example.com",
                "password1": "same.password@example.com",
                "password2": "same.password@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too similar")
        self.assertFalse(
            User.objects.filter(username="same.password@example.com").exists()
        )

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
    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="live")
    def test_missing_tinkoff_token_is_explicit(self):
        with self.assertRaisesMessage(MarketDataError, "TINKOFF_TOKEN"):
            from dashboard.services.market_data import get_client

            get_client()

        share = make_share()
        with self.assertRaisesMessage(MarketDataError, "TINKOFF_TOKEN"):
            get_last_data(share)
        self.assertFalse(PriceObservation.objects.exists())

        with self.assertRaisesMessage(CommandError, "TINKOFF_TOKEN"):
            call_command("add_shares")

        with self.assertRaisesMessage(CommandError, "PRICE_DATA_MODE=live"):
            call_command("collect_prices", max_cycles=1)

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
        observation = share.price_observations.get()
        self.assertEqual(observation.price, Decimal("12.345000000"))
        self.assertEqual(observation.observed_at, now)
        self.assertEqual(observation.currency, share.currency)
        self.assertEqual(observation.source, PriceObservation.Source.TINKOFF)
        self.assertGreaterEqual(observation.received_at, observation.observed_at)

    @override_settings(TINKOFF_TOKEN="test-token", PRICE_DATA_MODE="live")
    def test_price_observations_deduplicate_same_event_not_same_price(self):
        share = make_share()
        first_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second_timestamp = first_timestamp + timedelta(minutes=5)
        response = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    price=SimpleNamespace(units=12, nano=345_000_001),
                    time=first_timestamp,
                )
            ]
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.market_data.get_last_prices.side_effect = [response, response]
        response2 = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    price=SimpleNamespace(units=12, nano=345_000_001),
                    time=second_timestamp,
                )
            ]
        )
        client.market_data.get_last_prices.side_effect = [
            response,
            response,
            response2,
        ]

        with patch(
            "dashboard.services.market_data.get_client", return_value=client_context
        ):
            get_last_data(share)
            get_last_data(share)
            get_last_data(share)

        observations = list(share.price_observations.order_by("observed_at"))
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            [item.price for item in observations],
            [Decimal("12.345000001"), Decimal("12.345000001")],
        )

    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="demo")
    def test_demo_mode_records_explicitly_labeled_synthetic_observations(self):
        share = make_share(last_price=100.0)

        first_price, first_time = get_last_data(share)
        share.last_price = first_price
        second_price, second_time = get_last_data(share)

        observations = list(share.price_observations.order_by("observed_at"))
        self.assertEqual([item.price for item in observations], [
            Decimal("100.250000000"),
            Decimal("100.500000000"),
        ])
        self.assertEqual(
            {item.source for item in observations},
            {PriceObservation.Source.DEMO},
        )
        self.assertEqual(first_price, 100.25)
        self.assertEqual(second_price, 100.5)
        self.assertLess(first_time, second_time)
        self.assertTrue(all(item.currency == share.currency for item in observations))

    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="demo")
    def test_demo_observations_remain_distinct_when_clock_repeats(self):
        share = make_share(last_price=100.0)
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        with patch(
            "dashboard.services.market_data.timezone.now",
            side_effect=[now, now + timedelta(milliseconds=1), now, now + timedelta(milliseconds=2)],
        ):
            first_price, _ = get_last_data(share)
            share.last_price = first_price
            second_price, _ = get_last_data(share)

        observations = list(share.price_observations.order_by("observed_at"))
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            observations[1].observed_at - observations[0].observed_at,
            timedelta(microseconds=1),
        )
        self.assertTrue(
            all(item.received_at >= item.observed_at for item in observations)
        )

    @override_settings(PRICE_DATA_MODE="demo")
    def test_collect_prices_command_runs_bounded_cycles_and_persists_each(self):
        share = make_share(last_price=None)
        output = StringIO()

        call_command(
            "collect_prices",
            max_cycles=3,
            interval=0.001,
            share_id=share.pk,
            stdout=output,
        )

        observations = list(share.price_observations.order_by("observed_at"))
        self.assertEqual(len(observations), 3)
        self.assertEqual(
            [item.price for item in observations],
            [
                Decimal("100.250000000"),
                Decimal("100.500000000"),
                Decimal("100.750000000"),
            ],
        )
        self.assertEqual(output.getvalue().count("Цикл"), 3)
        self.assertIn("Сбор цен завершён", output.getvalue())

    def test_collect_prices_command_rejects_invalid_cycle_count(self):
        with self.assertRaises(CommandError):
            call_command("collect_prices", max_cycles=0)


class PriceHistoryTests(TestCase):
    def setUp(self):
        self.share = make_share()

    def add_observation(self, price, observed_at, source=PriceObservation.Source.DEMO):
        return PriceObservation.objects.create(
            share=self.share,
            price=Decimal(price),
            currency=self.share.currency,
            source=source,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )

    def add_series_observation(
        self, price, observed_at, source, currency
    ):
        return PriceObservation.objects.create(
            share=self.share,
            price=Decimal(price),
            currency=currency,
            source=source,
            observed_at=observed_at,
            received_at=observed_at + timedelta(seconds=1),
        )

    def test_history_calculates_changes_and_orders_by_aware_quote_time(self):
        now = django_timezone.now()
        newest = self.add_observation("15.000000001", now)
        self.add_observation("12.500000001", now - timedelta(days=2))
        history = get_price_history(self.share, "30d", now=now)

        self.assertEqual(history.observations[0].observed_at, now - timedelta(days=2))
        self.assertEqual(history.observations[-1], newest)
        self.assertEqual(history.series[0].absolute_change, Decimal("2.500000000"))
        self.assertEqual(history.series[0].percentage_change, Decimal("20.00"))

    def test_history_handles_empty_and_single_point(self):
        now = django_timezone.now()
        empty = get_price_history(self.share, "7d", now=now)
        self.assertEqual(empty.observations, [])
        self.assertEqual(empty.series, [])

        self.add_observation("12.5", now)
        one = get_price_history(self.share, "7d", now=now)
        self.assertEqual(len(one.observations), 1)
        self.assertEqual(len(one.series), 1)
        self.assertIsNone(one.series[0].absolute_change)
        self.assertIsNone(one.series[0].percentage_change)

    def test_mixed_sources_and_currencies_get_independent_statistics(self):
        now = django_timezone.now()
        self.add_series_observation(
            "100", now - timedelta(minutes=3), PriceObservation.Source.DEMO, "usd"
        )
        self.add_series_observation(
            "250", now - timedelta(minutes=2), PriceObservation.Source.TINKOFF, "usd"
        )
        self.add_series_observation(
            "250", now - timedelta(minutes=1), PriceObservation.Source.TINKOFF, "rub"
        )
        history = get_price_history(self.share, "1d", now=now)

        summaries = {
            (item.source, item.currency): item for item in history.series
        }
        self.assertEqual(
            set(summaries),
            {
                (PriceObservation.Source.DEMO, "usd"),
                (PriceObservation.Source.TINKOFF, "usd"),
                (PriceObservation.Source.TINKOFF, "rub"),
            },
        )
        self.assertTrue(
            all(item.absolute_change is None for item in summaries.values())
        )
        self.assertTrue(
            all(item.percentage_change is None for item in summaries.values())
        )

    def test_statistics_use_only_same_source_and_currency(self):
        now = django_timezone.now()
        self.add_series_observation(
            "100", now - timedelta(minutes=3), PriceObservation.Source.DEMO, "usd"
        )
        self.add_series_observation(
            "250", now - timedelta(minutes=2), PriceObservation.Source.TINKOFF, "usd"
        )
        self.add_series_observation(
            "250", now - timedelta(minutes=1), PriceObservation.Source.DEMO, "usd"
        )
        self.add_series_observation(
            "400", now, PriceObservation.Source.DEMO, "rub"
        )
        history = get_price_history(self.share, "1d", now=now)

        demo_usd = next(
            item
            for item in history.series
            if item.source == PriceObservation.Source.DEMO and item.currency == "usd"
        )
        self.assertEqual(demo_usd.absolute_change, Decimal("150"))
        self.assertEqual(demo_usd.percentage_change, Decimal("150.00"))
        self.assertEqual(
            len(next(item for item in history.series if item.currency == "rub").observations),
            1,
        )

    def test_zero_start_price_has_absolute_but_no_percentage_change(self):
        now = django_timezone.now()
        self.add_observation("0", now - timedelta(minutes=1))
        self.add_observation("2.5", now)

        history = get_price_history(self.share, "1d", now=now)
        self.assertEqual(history.series[0].absolute_change, Decimal("2.500000000"))
        self.assertIsNone(history.series[0].percentage_change)

    def test_history_period_filters_old_and_future_observations(self):
        now = django_timezone.now()
        self.add_observation("1", now - timedelta(days=8))
        inside = self.add_observation("2", now - timedelta(days=6))
        self.add_observation("3", now + timedelta(seconds=1))

        history = get_price_history(self.share, "7d", now=now)
        self.assertEqual(history.observations, [inside])
        self.assertEqual(
            len(get_price_history(self.share, "all", now=now).observations),
            2,
        )
        with self.assertRaises(ValueError):
            get_price_history(self.share, "invalid", now=now)

    def test_history_screen_handles_periods_sources_and_missing_instrument(self):
        now = django_timezone.now()
        self.add_observation("12", now - timedelta(hours=1))
        self.add_observation("13", now)
        self.add_series_observation(
            "1500", now - timedelta(minutes=30), PriceObservation.Source.TINKOFF, "rub"
        )

        response = self.client.get(
            reverse("dashboard:history", args=(self.share.pk,)),
            {"period": "1d"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Демонстрационные данные")
        self.assertContains(response, "не являются реальными котировками")
        self.assertContains(response, "Изменение в процентах")
        self.assertContains(response, "Демонстрационные данные — usd")
        self.assertContains(response, "Изменение: 1.000000000 usd")
        self.assertContains(response, "Tinkoff Invest — rub")
        self.assertContains(response, "Для этой серии изменение не рассчитывается")
        self.assertContains(response, "12.000000000")

        invalid = self.client.get(
            reverse("dashboard:history", args=(self.share.pk,)),
            {"period": "tomorrow"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(
            invalid,
            "Неизвестный период истории цен",
            status_code=400,
        )

        missing = self.client.get(
            reverse("dashboard:history", args=(999999,))
        )
        self.assertEqual(missing.status_code, 404)

    def test_history_graph_has_separate_trace_for_each_comparable_series(self):
        from dashboard.services.price_history import build_price_history_graph

        now = django_timezone.now()
        self.add_series_observation(
            "100", now - timedelta(minutes=2), PriceObservation.Source.DEMO, "usd"
        )
        self.add_series_observation(
            "250", now - timedelta(minutes=1), PriceObservation.Source.TINKOFF, "rub"
        )
        history = get_price_history(self.share, "1d", now=now)
        with patch(
            "dashboard.services.price_history.plot",
            return_value="<div>series chart</div>",
        ) as plot:
            graph = build_price_history_graph(history.series)

        self.assertEqual(graph, "<div>series chart</div>")
        traces = plot.call_args.args[0]
        self.assertEqual(len(traces), 2)
        self.assertEqual(
            {trace.name for trace in traces},
            {"Демонстрационные данные (usd)", "Tinkoff Invest (rub)"},
        )
        self.assertEqual({tuple(trace.y) for trace in traces}, {(100.0,), (250.0,)})

    @override_settings(PRICE_DATA_MODE="live", TINKOFF_TOKEN="")
    def test_empty_history_screen_reports_live_source_unavailable(self):
        response = self.client.get(
            reverse("dashboard:history", args=(self.share.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Живые котировки недоступны")
        self.assertContains(response, "сохранённых наблюдений нет")


class MarketDataIntegrationTests(TestCase):
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
        self.assertIsNone(existing.last_price)
        self.assertIsNone(existing.last_price_source)

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_catalog_keeps_distinct_instruments_with_shared_nonunique_isin(self):
        from dashboard.management.commands.add_shares import Command

        remote_shares = [
            SimpleNamespace(
                figi="IMPORT-A",
                uid="UID-A",
                ticker="IMPA",
                isin="SHARED-ISIN",
                name="Instrument A",
                currency="usd",
            ),
            SimpleNamespace(
                figi="IMPORT-B",
                uid="UID-B",
                ticker="IMPB",
                isin="SHARED-ISIN",
                name="Instrument B",
                currency="usd",
            ),
        ]
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = remote_shares

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            Command().handle()
            Command().handle()

        self.assertEqual(Share.objects.count(), 2)
        imported = {
            share.figi: (share.uid, share.ticker, share.isin, share.name)
            for share in Share.objects.order_by("figi")
        }
        self.assertEqual(
            imported,
            {
                "IMPORT-A": ("UID-A", "IMPA", "SHARED-ISIN", "Instrument A"),
                "IMPORT-B": ("UID-B", "IMPB", "SHARED-ISIN", "Instrument B"),
            },
        )

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_catalog_conflicting_figi_and_uid_fails_without_merging_rows(self):
        first = make_share()
        second = make_share(
            figi="BBG000B9XRY5",
            isin="US0378331004",
            name="Second",
            ticker="SECON",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e39",
        )
        remote_share = SimpleNamespace(
            figi=first.figi,
            uid=second.uid,
            ticker="CONFL",
            isin="SHARED-ISIN",
            name="Conflicted catalog row",
            currency="rub",
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            with self.assertRaisesMessage(CommandError, "FIGI и UID указывают"):
                call_command("add_shares")

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(Share.objects.count(), 2)
        self.assertEqual(first.figi, "BBG000B9XRY4")
        self.assertEqual(first.uid, "a9eb4238-eba9-488c-b102-b6140fd08e38")
        self.assertEqual(second.figi, "BBG000B9XRY5")
        self.assertEqual(second.uid, "a9eb4238-eba9-488c-b102-b6140fd08e39")

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_catalog_currency_change_invalidates_cache_and_keeps_old_observation(self):
        existing = make_share(
            last_price=100,
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        observation = PriceObservation.objects.create(
            share=existing,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        remote_share = SimpleNamespace(
            figi=existing.figi,
            isin=existing.isin,
            name="Apple Inc.",
            ticker=existing.ticker,
            currency="rub",
            uid=existing.uid,
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            call_command("add_shares")

        existing.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual(existing.currency, "rub")
        self.assertIsNone(existing.last_price)
        self.assertIsNone(existing.last_price_source)
        self.assertIsNone(existing.last_price_observed_at)
        self.assertEqual(observation.currency, "usd")
        self.assertEqual(observation.price, Decimal("100"))

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_catalog_identity_change_with_history_fails_without_rewriting_data(self):
        existing = make_share(
            last_price=100,
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        observation = PriceObservation.objects.create(
            share=existing,
            price=Decimal("100"),
            currency="usd",
            source=PriceObservation.Source.TINKOFF,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        remote_share = SimpleNamespace(
            figi="BBG000B9XRY9",
            isin=existing.isin,
            name="Apple Inc.",
            ticker=existing.ticker,
            currency=existing.currency,
            uid=existing.uid,
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            with self.assertRaisesMessage(CommandError, "сохранённой историей"):
                call_command("add_shares")

        existing.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual(existing.figi, "BBG000B9XRY4")
        self.assertEqual(existing.last_price, 100)
        self.assertEqual(observation.share_id, existing.pk)
        self.assertEqual(observation.currency, "usd")
        self.assertEqual(observation.price, Decimal("100"))

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_catalog_cannot_replace_identity_with_open_portfolio_position(self):
        existing = make_share()
        owner = User.objects.create_user(
            username="catalog-portfolio@example.com",
            email="catalog-portfolio@example.com",
            password="test-password-for-local-suite",
        )
        portfolio = VirtualPortfolio.objects.create(
            owner=owner,
            name="Catalog protection",
            currency="USD",
            valuation_source=PriceObservation.Source.DEMO,
            starting_cash=Decimal("1000"),
            cash_balance=Decimal("1000"),
        )
        execute_portfolio_transaction(
            portfolio_id=portfolio.pk,
            share_id=existing.pk,
            side=PortfolioTransaction.Side.BUY,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            commission=Decimal("0"),
            currency="USD",
            executed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            idempotency_key=uuid4(),
        )
        remote_share = SimpleNamespace(
            figi=existing.figi,
            isin=existing.isin,
            name="Replacement instrument",
            ticker="REPL",
            currency="usd",
            uid="REPLACEMENT-UID",
        )
        client_context = MagicMock()
        client = client_context.__enter__.return_value
        client.instruments.shares.return_value.instruments = [remote_share]

        with patch(
            "dashboard.management.commands.add_shares.get_client",
            return_value=client_context,
        ):
            with self.assertRaisesMessage(CommandError, "сохранённой историей"):
                call_command("add_shares")

        existing.refresh_from_db()
        position = portfolio.positions.get()
        transaction_record = portfolio.transactions.get()
        self.assertEqual(existing.figi, "BBG000B9XRY4")
        self.assertEqual(existing.uid, "a9eb4238-eba9-488c-b102-b6140fd08e38")
        self.assertEqual(position.quantity, Decimal("1.000000000"))
        self.assertEqual(transaction_record.share_ticker, "AAPL")
        self.assertFalse(existing.price_observations.exists())

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
        self.assertEqual(share.last_price_source, Share.PriceSource.TINKOFF)
        self.assertIsNotNone(share.last_price_observed_at)
        self.assertGreater(share.updated_at, old_timestamp)

    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="demo")
    def test_demo_collection_does_not_overwrite_live_price_cache(self):
        from dashboard.tasks import update_last_price

        observed_at = datetime(2026, 10, 24, tzinfo=timezone.utc)
        share = make_share(
            last_price=88.5,
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=observed_at,
        )
        with patch(
            "dashboard.tasks.get_last_data",
            return_value=(100.25, observed_at + timedelta(minutes=1)),
        ):
            update_last_price.run()

        share.refresh_from_db()
        self.assertEqual(share.last_price, 88.5)
        self.assertEqual(share.last_price_source, Share.PriceSource.TINKOFF)
        self.assertEqual(share.last_price_observed_at, observed_at)
        response = APIClient().get(f"/api/v1/shares/{share.pk}/")
        self.assertEqual(response.data["last_price"], 88.5)
        self.assertEqual(response.data["last_price_source"], Share.PriceSource.TINKOFF)
        self.assertEqual(
            response.data["last_price_observed_at"],
            observed_at.isoformat().replace("+00:00", "Z"),
        )

    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="live")
    def test_live_failure_preserves_price_and_provenance_after_demo_setting(self):
        from dashboard.tasks import update_last_price

        observed_at = datetime(2026, 10, 24, tzinfo=timezone.utc)
        share = make_share(
            last_price=88.5,
            last_price_source=Share.PriceSource.TINKOFF,
            last_price_observed_at=observed_at,
        )
        update_last_price.run()

        share.refresh_from_db()
        self.assertEqual(share.last_price, 88.5)
        self.assertEqual(share.last_price_source, Share.PriceSource.TINKOFF)
        self.assertEqual(share.last_price_observed_at, observed_at)

    @override_settings(TINKOFF_TOKEN="", PRICE_DATA_MODE="live")
    def test_unproven_cached_price_is_not_reported_as_live_after_mode_switch(self):
        from dashboard.tasks import update_last_price

        share = make_share(
            last_price=100.25,
            last_price_source=None,
            last_price_observed_at=None,
        )
        update_last_price.run()

        response = APIClient().get(f"/api/v1/shares/{share.pk}/")
        self.assertEqual(response.data["last_price"], 100.25)
        self.assertEqual(response.data["last_price_source"], "unknown")
        self.assertIsNone(response.data["last_price_observed_at"])
        detail = self.client.get(reverse("dashboard:detail", args=(share.pk,)))
        self.assertContains(detail, "Источник последней цены не подтверждён")

    @override_settings(PRICE_DATA_MODE="live")
    def test_price_collection_stops_between_shares_when_event_is_set(self):
        from dashboard.tasks import _update_last_price

        first = make_share()
        second = make_share(
            figi="BBG000B9XRY5",
            isin="US0378331004",
            name="Second",
            ticker="SECON",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e39",
        )
        third = make_share(
            figi="BBG000B9XRY6",
            isin="US0378331003",
            name="Third",
            ticker="THIRD",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e40",
        )
        self.assertEqual(Share.objects.count(), 3)
        stop_event = Event()

        def finish_current_request(_share):
            stop_event.set()
            return 101.0, datetime.now(timezone.utc)

        with patch(
            "dashboard.tasks.get_last_data", side_effect=finish_current_request
        ) as get_last_data_mock:
            updated = _update_last_price(stop_event=stop_event)

        self.assertEqual(updated, 1)
        self.assertEqual(get_last_data_mock.call_count, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(first.last_price, 101.0)
        self.assertEqual(second.last_price, 100.5)
        self.assertEqual(third.last_price, 100.5)

    @override_settings(TINKOFF_TOKEN="test-token")
    def test_first_price_has_unknown_trend(self):
        from dashboard.tasks import update_last_price

        share = make_share(last_price=None, is_trend_high=None)
        with patch("dashboard.tasks.get_last_data", return_value=(100.0, None)):
            update_last_price.run()
        share.refresh_from_db()
        self.assertEqual(share.last_price, 100.0)
        self.assertIsNone(share.is_trend_high)
