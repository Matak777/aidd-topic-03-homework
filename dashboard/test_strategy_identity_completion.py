"""An existing strategy must not silently start trading a different asset."""

from django.urls import reverse

from dashboard.api.v1.serializer import ShareSerializer
from dashboard.forms import ShareModelForm
from dashboard.tests import StrategyBase


class StrategyInstrumentIdentityTests(StrategyBase):
    def test_strategy_protects_identity_before_the_first_quote(self):
        self.make_strat()
        self.assertFalse(self.share.price_observations.exists())
        self.assertFalse(self.share.trades.exists())
        desired = {
            "figi": "DIFFERENT-FIGI",
            "uid": "DIFFERENT-UID",
            "ticker": "DIFFERENT",
            "currency": "EUR",
        }
        self.assertEqual(
            set(self.share.protected_identity_changes(desired)), set(desired)
        )
        serializer = ShareSerializer(
            self.share, data={"figi": desired["figi"]}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            serializer.save()
        self.share.refresh_from_db()
        self.assertNotEqual(self.share.figi, desired["figi"])

    def test_rename_remains_available_but_html_identity_change_is_rejected(
        self,
    ):
        self.make_strat()
        self.client.force_login(self.user)
        data = {
            "figi": self.share.figi,
            "uid": self.share.uid,
            "isin": self.share.isin,
            "ticker": self.share.ticker,
            "currency": self.share.currency,
            "name": "Renamed safely",
        }
        form = ShareModelForm(data, instance=self.share)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        response = self.client.post(
            reverse("dashboard:update", args=[self.share.pk]),
            data | {"ticker": "OTHER"},
        )
        self.assertEqual(response.status_code, 200)
        self.share.refresh_from_db()
        self.assertEqual(self.share.name, "Renamed safely")
        self.assertNotEqual(self.share.ticker, "OTHER")
