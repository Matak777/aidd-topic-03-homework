import threading
import time
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

import dashboard.tasks as tasks
from dashboard.admin import ShareAdmin, ShareAdminForm
from dashboard.models import PriceObservation, Share


class DashboardTests(APITestCase):
    def setUp(self) -> None:
        self.apple_share = Share.objects.create(
            figi="BBG000B9XRY4",
            isin="US0378331005",
            name="Apple",
            ticker="AAPL",
            currency="usd",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e38",
            last_price=100.50,
            is_trend_high=True,
        )

        self.second_company = {
            "figi": "BBG000B9XRY5",
            "isin": "US0378331004",
            "name": "Company_test",
            "ticker": "TESTTEST",
            "currency": "usd",
            "uid": "a9eb4238-eba9-488c-b102-b6140fd08e39",
            "last_price": 1.50,
            "is_trend_high": False,
        }

        # API-запись (POST/PUT/PATCH/DELETE) разрешена только
        # аутентифицированным; чтение — публично (IsAuthenticatedOrReadOnly)
        self.user = get_user_model().objects.create_user(
            "writer@example.com", "writer@example.com", "N00c2_f_k_correct"
        )

    def test_share_list_html(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_share_list(self):
        response = self.client.get("/api/v1/shares/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        apple_share = response.json().get("results")[0]
        self.assertEqual(apple_share["id"], self.apple_share.id)
        self.assertEqual(apple_share["figi"], self.apple_share.figi)
        self.assertEqual(apple_share["isin"], self.apple_share.isin)
        self.assertEqual(apple_share["name"], self.apple_share.name)
        self.assertEqual(apple_share["ticker"], self.apple_share.ticker)
        self.assertEqual(apple_share["currency"], self.apple_share.currency)

    def test_share_detail(self):
        response = self.client.get("/api/v1/shares/")
        share_detail = response.json().get("results")[0]
        response = self.client.get(f"/api/v1/shares/{share_detail['id']}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_share_detail_invalid(self):
        response = self.client.get("/api/v1/shares/2/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_share_update(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/shares/")
        share_detail = response.json().get("results")[0]
        response = self.client.put(
            f"/api/v1/shares/{share_detail['id']}/",
            {
                "id": share_detail["id"],
                "figi": self.apple_share.figi,
                "isin": self.apple_share.isin,
                "name": f"{self.apple_share.name}_test",
                "ticker": self.apple_share.ticker,
                "currency": self.apple_share.currency,
                "uid": self.apple_share.uid,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["figi"], self.apple_share.figi)
        self.assertEqual(body["name"], f"{self.apple_share.name}_test")
        self.assertEqual(body["uid"], self.apple_share.uid)

    def test_share_update_invalid(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/shares/")
        share_detail = response.json().get("results")[0]
        response = self.client.put(
            f"/api/v1/shares/{share_detail['id']}/",
            {
                "figi": self.apple_share.figi,
                "name": f"{self.apple_share.name}_test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_share_create(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/shares/", self.second_company, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["figi"], self.second_company["figi"])
        self.assertEqual(body["name"], self.second_company["name"])
        self.assertEqual(body["ticker"], self.second_company["ticker"])
        # uid включён в API create/read (см. MODERNIZATION: без него каждый
        # второй POST падал с IntegrityError 500)
        self.assertEqual(body["uid"], self.second_company["uid"])

        response = self.client.get("/api/v1/shares/")
        self.assertEqual(len(response.data["results"]), 2)

    def test_share_create_invalid(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/shares/", {"figi": "BBG000B9XRY5"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_share_create_missing_uid_is_400_not_500(self):
        # uid уникален в БД и обязателен: пустой запрос даёт понятную 400,
        # а не IntegrityError 500 (скрытый дефект исходного API)
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/shares/",
            {
                "figi": "BBG000B9XRY6",
                "isin": "US02",
                "name": "NoUid",
                "ticker": "NOUID",
                "currency": "usd",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("uid", response.json())

    def test_share_create_duplicate_uid_400_not_500(self):
        # повтор uid уже существующей акции — 400 с валидатором уникальности
        self.client.force_login(self.user)
        response = self.client.post(
            "/api/v1/shares/",
            {
                **self.second_company,
                "uid": self.apple_share.uid,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("uid", response.json())

    def test_share_delete(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/shares/")
        share_detail = response.json().get("results")[0]
        response = self.client.delete(f"/api/v1/shares/{share_detail['id']}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        response = self.client.get("/api/v1/shares/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

    def test_share_delete_invalid(self):
        self.client.force_login(self.user)
        response = self.client.delete("/api/v1/shares/3/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AnonymousApiPermissionTests(TestCase):
    """Чтение API публично; запись — только аутентифицированным (Д12)."""

    def setUp(self):
        Share.objects.create(
            figi="BBG000B9XRY4",
            isin="US0378331005",
            name="Apple",
            ticker="AAPL",
            currency="usd",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e38",
        )
        self.write_payload = {
            "figi": "BBG000B9XRY5",
            "isin": "US0378331004",
            "name": "NoName",
            "ticker": "NOAUTH",
            "currency": "usd",
            "uid": "00000000-0000-0000-0000-000000000001",
        }

    def test_read_is_public(self):
        self.assertEqual(self.client.get("/api/v1/shares/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/shares/1/").status_code, 200)

    def test_anonymous_post_is_403(self):
        response = self.client.post(
            "/api/v1/shares/", self.write_payload, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Share.objects.exclude(figi="BBG000B9XRY4").exists())

    def test_anonymous_put_is_403(self):
        response = self.client.put(
            "/api/v1/shares/1/",
            self.write_payload | {"name": "Changed"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_patch_is_403(self):
        response = self.client.patch(
            "/api/v1/shares/1/", {"name": "Changed"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Share.objects.first().name, "Apple")

    def test_anonymous_delete_is_403(self):
        response = self.client.delete("/api/v1/shares/1/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Share.objects.filter(figi="BBG000B9XRY4").exists())


class AuthenticatedApiWriteTests(TestCase):
    """Вошедший пользователь может писать в API."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "writer@example.com", "writer@example.com", "N00c2_f_k_correct"
        )
        self.client.force_login(self.user)

    def test_write_roundtrip(self):
        import json

        payload = {
            "figi": "BBG000B9XRY5",
            "isin": "US0378331004",
            "name": "Created By Login",
            "ticker": "LOGIN",
            "currency": "usd",
            "uid": "11111111-1111-1111-1111-111111111111",
        }
        response = self.client.post(
            "/api/v1/shares/",
            json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.patch(
            "/api/v1/shares/1/",
            json.dumps({"name": "Patched"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Share.objects.get(pk=1).name, "Patched")
        response = self.client.delete("/api/v1/shares/1/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(Share.objects.count(), 0)


class DeadlineTransportTests(TestCase):
    """Реальный gRPC-дедлайн на подменённом транспорте (без живого токена).

    Поднимается локальный grpc-сервер с вендорным сервисом Tinkoff; RPC
    выполняется через SDK-сервисы с обёрткой DeadlineStub.
    """

    SLEEP_S = 2.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from concurrent import futures

        import grpc
        from tinkoff.invest.grpc import (
            common_pb2,
            marketdata_pb2,
        )
        from tinkoff.invest.grpc import (
            marketdata_pb2_grpc as marketdata_pb2_grpc_module,
        )

        class LastPricesServicer(
            marketdata_pb2_grpc_module.MarketDataServiceServicer
        ):
            def __init__(self):
                self.sleep = cls.SLEEP_S
                self.fast = marketdata_pb2.GetLastPricesResponse(
                    last_prices=[
                        marketdata_pb2.LastPrice(
                            figi="BBG000B9XRY4",
                            price=common_pb2.Quotation(units=7, nano=20000000),
                        )
                    ]
                )

            def GetLastPrices(self, request, context):
                if self.sleep > 0:
                    time.sleep(self.sleep)
                    self.sleep = 0  # последующие вызовы этого объекта быстрые
                return self.fast

        cls.servicer = LastPricesServicer()
        cls.grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=2)
        )
        marketdata_pb2_grpc_module.add_MarketDataServiceServicer_to_server(
            cls.servicer, cls.grpc_server
        )
        port = cls.grpc_server.add_insecure_port("127.0.0.1:0")
        cls.grpc_server.start()
        cls.target = f"127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.grpc_server.stop(0)
        super().tearDownClass()

    def _services(self):
        import grpc
        from tinkoff.invest.services import Services

        channel = grpc.insecure_channel(self.target)
        return channel, Services(channel, token="test-token")

    def test_deadline_exceeded_on_slow_rpc(self):
        import grpc
        from tinkoff.invest.exceptions import RequestError

        from dashboard.services.market_data import apply_rpc_deadline

        channel, services = self._services()
        try:
            apply_rpc_deadline(services, 0.3)
            started = time.monotonic()
            with self.assertRaises(RequestError) as ctx:
                services.market_data.get_last_prices(figi=["BBG000B9XRY4"])
            self.assertEqual(
                ctx.exception.code, grpc.StatusCode.DEADLINE_EXCEEDED
            )
            elapsed = time.monotonic() - started
            # RPC реально прерван дедлайном: дождались ~0.3 с, не 2 с
            self.assertLess(elapsed, 1.5)
        finally:
            channel.close()

    def test_fast_rpc_passes_and_returns_prices(self):
        from dashboard.services.market_data import apply_rpc_deadline

        channel, services = self._services()
        try:
            apply_rpc_deadline(services, 1.0)
            self.servicer.sleep = 0
            resp = services.market_data.get_last_prices(figi=["BBG000B9XRY4"])
            item = resp.last_prices[0]
            self.assertEqual(item.price.units, 7)
        finally:
            channel.close()

    def test_no_background_thread_accumulation(self):
        import grpc
        from tinkoff.invest.exceptions import RequestError

        from dashboard.services.market_data import apply_rpc_deadline

        thread_count_before = threading.active_count()
        for _ in range(3):
            self.servicer.sleep = self.SLEEP_S
            channel, services = self._services()
            try:
                apply_rpc_deadline(services, 0.2)
                with self.assertRaises(RequestError) as ctx:
                    services.market_data.get_last_prices(figi=["BBG000B9XRY4"])
                self.assertEqual(
                    ctx.exception.code, grpc.StatusCode.DEADLINE_EXCEEDED
                )
            finally:
                channel.close()
        time.sleep(0.3)
        self.assertLessEqual(threading.active_count() - thread_count_before, 1)

    def test_run_with_timeout_surfaces_deadline_exceeded(self):
        # get_last_prices через подменённый транспорт возвращает
        # RequestError(DEADLINE_EXCEEDED), а не подвисание
        import grpc
        from tinkoff.invest.exceptions import RequestError

        import dashboard.services.market_data as md

        channel, services = self._services()
        self.servicer.sleep = self.SLEEP_S
        try:
            md.apply_rpc_deadline(services, 0.3)
            with patch.object(md, "open_tinkoff_client") as mock_open:
                mock_open.return_value = _CtxManager(channel, services)
                with self.assertRaises(RequestError) as ctx:
                    md.get_last_prices(["BBG000B9XRY4"], timeout_s=0.3)
            self.assertEqual(
                ctx.exception.code, grpc.StatusCode.DEADLINE_EXCEEDED
            )
        finally:
            self.servicer.sleep = 0
            channel.close()


class _CtxManager:
    def __init__(self, channel, services):
        self.channel = channel
        self.services = services

    def __enter__(self):
        return self.services

    def __exit__(self, exc_type, exc, tb):
        return False


SHARE_DICT = {
    "figi": "BBG000BLN4K2",
    "isin": "US0231351067",
    "name": "Amazon",
    "ticker": "AMZN2",
    "currency": "usd",
    "uid": "b9eb4238-eba9-488c-b102-b6140fd08e99",
}


class DashboardHtmlTests(APITestCase):
    def _create_user(
        self, username="user@example.com", password="N00c2_f_k_correct"
    ):
        user_model = get_user_model()
        return user_model.objects.create_user(username, username, password)

    def _login(self, **kwargs):
        user = self._create_user(**kwargs)
        self.client.force_login(user)
        return user

    def test_list_page_requires_no_auth(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Акций еще нет", response.content.decode())

    def test_list_highlight_true_false(self):
        Share.objects.create(**SHARE_DICT, last_price=1.0, is_trend_high=True)
        Share.objects.create(
            figi="B1",
            ticker="A1",
            uid="u1",
            last_price=1.0,
            is_trend_high=False,
        )
        self._login()
        response = self.client.get(reverse("dashboard:index"))
        body = response.content.decode()
        self.assertIn("mediumseagreen", body)
        self.assertIn("palevioletred", body)

    def test_list_highlight_unknown_trend(self):
        Share.objects.create(**SHARE_DICT, last_price=None, is_trend_high=None)
        self._login()
        response = self.client.get(reverse("dashboard:index"))
        body = response.content.decode()
        # тренд неизвестен: подсветки нет в отличие от зелёного/красного
        self.assertNotIn("mediumseagreen", body)
        self.assertNotIn("palevioletred", body)

    def test_create_requires_auth(self):
        response = self.client.get(reverse("dashboard:create"))
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )

    def test_create_update_delete_by_authenticated_user(self):
        self._login()
        response = self.client.post(
            reverse("dashboard:create"), {**SHARE_DICT}, follow=True
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share = Share.objects.get(figi=SHARE_DICT["figi"])

        response = self.client.post(
            reverse("dashboard:update", args=[share.id]),
            {**SHARE_DICT, "name": "Amazon2"},
            follow=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Share.objects.get(id=share.id).name, "Amazon2")

        response = self.client.post(
            reverse("dashboard:delete", args=[share.id]), {}
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        self.assertFalse(Share.objects.filter(id=share.id).exists())

    def test_update_page_invalid_fields_does_not_500(self):
        self._login()
        share = Share.objects.create(**SHARE_DICT)
        response = self.client.post(
            reverse("dashboard:update", args=[share.id]), {"name": "x"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("form", response.context)

    def test_detail_unknown_share_404(self):
        response = self.client.get(reverse("dashboard:detail", args=[999]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("dashboard.views.get_graph")
    def test_detail_renders_graph_error_explicitly(self, graph_mock):
        graph_mock.side_effect = RuntimeError("TINKOFF_TOKEN не задан")
        Share.objects.create(**SHARE_DICT)
        response = self.client.get(reverse("dashboard:detail", args=[1]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("График недоступен", response.content.decode())

    @patch("dashboard.views.get_graph")
    def test_detail_renders_plot(self, graph_mock):
        graph_mock.return_value = "<div>plot</div>"
        Share.objects.create(**SHARE_DICT)
        response = self.client.get(reverse("dashboard:detail", args=[1]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("plot", response.content.decode())


class TinkoffServiceHelperTests(TestCase):
    def test_run_with_timeout_returns_result(self):
        from dashboard.services.market_data import run_with_timeout

        self.assertEqual(run_with_timeout(lambda: 41 + 1, 1.0, "test"), 42)

    def test_run_with_timeout_raises_original_error(self):
        from dashboard.services.market_data import run_with_timeout

        def boom():
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            run_with_timeout(boom, 1.0, "test")

    def test_run_with_timeout_exceeds_limit(self):
        from dashboard.services.market_data import run_with_timeout

        def sleepy():
            time.sleep(2.5)
            return "finished"

        # ожидание ограничено дедлайном + буфер; операция без собственного
        # дедлайна (hang) всё равно прекращается по превышении локального
        # лимита (см. реальный gRPC-дедлайн в DeadlineTransportTests)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            run_with_timeout(sleepy, 0.05, "test")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.0)

    def test_validate_timeout_strict(self):
        from dashboard.services.market_data import validate_timeout

        self.assertEqual(validate_timeout("2", "t"), 2.0)
        self.assertEqual(validate_timeout(0.5, "t"), 0.5)
        self.assertEqual(validate_timeout(float("1e-3"), "t"), 1e-3)
        for bad in (0, -1, -0.1, float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                validate_timeout(bad, "t")
        with self.assertRaises(ValueError):
            validate_timeout("abc", "t")
        with self.assertRaises(ValueError):
            validate_timeout(None, "t")


class UpdateLastPriceTaskTests(APITestCase):
    def _run(self, prices):
        # фиксируем время наблюдений mock-провайдера, чтобы дедупликация
        # (unique: share + source + quote_time) не зависела от clock
        now = timezone.now()
        prices = {
            figi: (price, quote_time if quote_time is not None else now)
            for figi, (price, quote_time) in prices.items()
        }
        with patch(
            "dashboard.tasks.price_history.fetch_prices", return_value=prices
        ):
            tasks.update_last_price()

    def test_creates_trend_from_first_price(self):
        s = Share.objects.create(
            **SHARE_DICT, last_price=None, is_trend_high=None
        )
        self._run({SHARE_DICT["figi"]: (10.0, None)})
        s.refresh_from_db()
        self.assertEqual(s.last_price, 10.0)
        self.assertIsNone(s.is_trend_high)
        # updated_at устанавливается даже при bulk-update (исправление Д5)
        self.assertIsNotNone(s.updated_at)

    def test_price_growth_vs_fall(self):
        s = Share.objects.create(
            **SHARE_DICT, last_price=5.0, is_trend_high=None
        )
        self._run({SHARE_DICT["figi"]: (10.0, None)})
        s.refresh_from_db()
        self.assertTrue(s.is_trend_high)
        self._run({SHARE_DICT["figi"]: (9.0, None)})
        s.refresh_from_db()
        self.assertFalse(s.is_trend_high)
        self.assertEqual(s.last_price, 9.0)

    def test_equal_price_is_not_growth(self):
        s = Share.objects.create(
            **SHARE_DICT, last_price=5.0, is_trend_high=None
        )
        self._run({SHARE_DICT["figi"]: (5.0, None)})
        s.refresh_from_db()
        self.assertFalse(s.is_trend_high)

    def test_get_not_touched_on_error(self):
        s = Share.objects.create(
            **SHARE_DICT, last_price=1.0, is_trend_high=True
        )
        with patch(
            "dashboard.tasks.price_history.fetch_prices",
            side_effect=RuntimeError("net"),
        ):
            result = tasks.update_last_price()
        self.assertIsNone(result)
        s.refresh_from_db()
        self.assertEqual(s.last_price, 1.0)
        self.assertTrue(s.is_trend_high)

    def test_share_without_price_is_skipped(self):
        s = Share.objects.create(
            **SHARE_DICT, last_price=1.0, is_trend_high=None
        )
        self._run({})  # API не вернул цену
        s.refresh_from_db()
        self.assertEqual(s.last_price, 1.0)
        self.assertIsNone(s.is_trend_high)


class AddSharesCommandTests(APITestCase):
    def _fake_ctx(self, client):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=client)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def _fake_api(self, instruments):
        client = MagicMock()
        client.instruments.shares.return_value = SimpleNamespace(
            instruments=instruments
        )
        return self._fake_ctx(client)

    def test_creates_shares(self):
        instr = SimpleNamespace(**SHARE_DICT)
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            return_value=self._fake_api([instr]),
        ):
            call_command("add_shares")
        self.assertTrue(Share.objects.filter(figi=SHARE_DICT["figi"]).exists())

    def test_changed_fields_update_instead_of_integrity_error(self):
        Share.objects.create(**{**SHARE_DICT, "ticker": "OLD"})
        changed = SimpleNamespace(**{**SHARE_DICT, "isin": "NEW-ISIN"})
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            return_value=self._fake_api([changed]),
        ):
            call_command("add_shares")
        share = Share.objects.get(figi=SHARE_DICT["figi"])
        self.assertEqual(share.ticker, SHARE_DICT["ticker"])
        self.assertEqual(share.isin, "NEW-ISIN")

    def test_token_missing_reports_error_not_crash(self):
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            side_effect=RuntimeError("TINKOFF_TOKEN не задан"),
        ):
            call_command("add_shares", stderr=StringIO(), stdout=StringIO())

    def test_network_error_of_instruments_shares_reports_not_crash(self):
        # Сбой самого внешнего запроса (не только создания клиента)
        # обрабатывается явно: сообщение в stderr, данных в БД нет.
        client = MagicMock()
        client.instruments.shares.side_effect = RuntimeError("network down")
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            return_value=self._fake_ctx(client),
        ):
            out, err = StringIO(), StringIO()
            call_command("add_shares", stdout=out, stderr=err)
        self.assertIn("network down", err.getvalue())
        self.assertFalse(Share.objects.exists())

    def test_instruments_shares_timeout_reports_not_crash(self):
        client = MagicMock()
        client.instruments.shares.side_effect = TimeoutError("timed out")
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            return_value=self._fake_ctx(client),
        ):
            out, err = StringIO(), StringIO()
            call_command("add_shares", stdout=out, stderr=err)
        self.assertIn("timed out", err.getvalue())
        self.assertFalse(Share.objects.exists())


# --------------------------------------------------------------------------
# Группа 3 задания №4: накопленная история цен (FEATURES.md F3.1–F3.5)
# --------------------------------------------------------------------------


class ExactPriceConversionTests(TestCase):
    """Точная цена: Quotation units/nano → Decimal без float."""

    def test_units_and_nano(self):
        from dashboard.services.market_data import quotation_to_decimal

        q = SimpleNamespace(units=123, nano=450_000_000)
        self.assertEqual(quotation_to_decimal(q), Decimal("123.45"))

    def test_negative_price(self):
        from dashboard.services.market_data import quotation_to_decimal

        q = SimpleNamespace(units=-2, nano=50_000_000)
        self.assertEqual(quotation_to_decimal(q), Decimal("-1.95"))

    def test_nano_not_lost(self):
        from dashboard.services.market_data import quotation_to_decimal

        q = SimpleNamespace(units=0, nano=1)
        self.assertEqual(quotation_to_decimal(q), Decimal("0.000000001"))


class RecordObservationDedupTests(TestCase):
    """Дедупликация повторной доставки наблюдения (F3.2)."""

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT)

    def _observations(self, source, quote_time, price=Decimal("100.5")):
        from dashboard.services.price_history import record_observations

        return record_observations(
            {self.share.figi: (price, quote_time)},
            [self.share],
            source,
        )

    def test_redelivery_of_same_observation_creates_one_row(self):
        quote_time = timezone.now() - timedelta(hours=1)
        first = self._observations("tinkoff", quote_time)
        second = self._observations("tinkoff", quote_time)
        self.assertEqual(first, 1)
        # повторная доставка того же наблюдения — идемпотентна
        self.assertEqual(second, 0)
        self.assertEqual(self.share.price_observations.count(), 1)

    def test_same_price_different_time_preserved(self):
        t1 = timezone.now() - timedelta(hours=2)
        t2 = timezone.now() - timedelta(hours=1)
        self._observations("tinkoff", t1)
        self._observations("tinkoff", t2)
        rows = list(self.share.price_observations.order_by("quote_time"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].price_decimal, rows[1].price_decimal)

    def test_quote_time_not_overwritten_by_received_at(self):
        quote_time = timezone.now() - timedelta(days=365)
        self._observations("tinkoff", quote_time)
        observation = self.share.price_observations.get()
        self.assertEqual(observation.quote_time, quote_time)
        self.assertGreater(observation.received_at, observation.quote_time)

    def test_currency_recorded_from_share(self):
        self._observations("tinkoff", timezone.now())
        observation = self.share.price_observations.get()
        # валюта наблюдения фиксируется из инструмента (SHARE_DICT: usd)
        self.assertEqual(observation.currency, "usd")

    def test_unknown_source_rejected(self):
        from dashboard.services.price_history import record_observations

        with self.assertRaises(ValueError):
            record_observations(
                {self.share.figi: (Decimal("1.0"), timezone.now())},
                [self.share],
                "mystery",
            )

    def test_missing_price_or_time_skipped(self):
        from dashboard.services.price_history import record_observations

        created = record_observations(
            {self.share.figi: (None, timezone.now())}, [self.share], "demo"
        )
        created += record_observations(
            {self.share.figi: (Decimal("10"), None)}, [self.share], "demo"
        )
        self.assertEqual(created, 0)
        self.assertEqual(self.share.price_observations.count(), 0)


class GetLastPricesDecimalTests(TestCase):
    """Реальный путь get_last_prices возвращает Decimal из units/nano."""

    def test_decimal_returned_from_rpc_response(self):
        from dashboard.services.market_data import get_last_prices

        response = SimpleNamespace(
            last_prices=[
                SimpleNamespace(
                    figi=SHARE_DICT["figi"],
                    price=SimpleNamespace(units=123, nano=450_000_000),
                    time=timezone.now(),
                )
            ]
        )
        client = MagicMock()
        client.market_data.get_last_prices.return_value = response
        # _CtxManager(None, client): __enter__ возвращает второй аргумент —
        # здесь подменённый клиент SDK
        with patch(
            "dashboard.services.market_data.open_tinkoff_client",
            return_value=_CtxManager(None, client),
        ):
            result = get_last_prices([SHARE_DICT["figi"]])
        price, quote_time = result[SHARE_DICT["figi"]]
        self.assertEqual(price, Decimal("123.45"))
        self.assertIsInstance(price, Decimal)


class PeriodIntervalValidationTests(TestCase):
    def test_known_periods(self):
        from dashboard.services.price_history import (
            validate_period,
        )

        self.assertEqual(validate_period("1d"), 1)
        self.assertIsNone(validate_period("all"))

    def test_unknown_period_raises(self):
        from dashboard.services.price_history import validate_period

        with self.assertRaises(ValueError):
            validate_period("2h")

    def test_validate_interval(self):
        from dashboard.services.price_history import validate_interval

        self.assertEqual(validate_interval("2", "t"), 2.0)
        for bad in (0, -1, float("nan"), float("inf"), "abc"):
            with self.assertRaises(ValueError):
                validate_interval(bad, "t")


class DemoProviderTests(TestCase):
    """Demo-источник: синтетика, Decimal, явно помечен как демонстрация."""

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT, last_price=150.0)

    def test_prices_are_positive_decimals_for_every_share(self):
        from dashboard.services.price_history import demo_prices

        result = demo_prices([self.share])
        price, quote_time = result[self.share.figi]
        self.assertIsInstance(price, Decimal)
        self.assertGreater(price, 0)
        self.assertIsNotNone(quote_time)

    def test_demo_collect_records_with_demo_source(self):
        from dashboard.services.price_history import collect_cycle

        created, total = collect_cycle(source="demo")
        self.assertEqual(created, 1)
        self.assertEqual(total, 1)
        observation = self.share.price_observations.get()
        self.assertEqual(observation.source, "demo")
        self.assertEqual(observation.currency, "usd")


class CollectPricesCommandTests(TestCase):
    """Локальный периодический коллектор без Redis (F3.3)."""

    def setUp(self):
        Share.objects.create(**SHARE_DICT, last_price=100.0)

    def test_demo_collection_accumulates_limited_cycles(self):
        out, err = StringIO(), StringIO()
        call_command(
            "collect_prices",
            source="demo",
            interval=0.01,
            max_cycles=3,
            stdout=out,
            stderr=err,
        )
        text = out.getvalue()
        self.assertIn("лимит циклов достигнут", text)
        self.assertNotIn("сбой цикла", err.getvalue())
        self.assertEqual(self._demo_rows().count(), 3)

    def _demo_rows(self):
        share = Share.objects.get(figi=SHARE_DICT["figi"])
        return share.price_observations.filter(source="demo")

    def test_tinkoff_without_token_reports_and_stays_alive(self):
        out, err = StringIO(), StringIO()
        call_command(
            "collect_prices",
            source="tinkoff",
            interval=0.01,
            max_cycles=2,
            stdout=out,
            stderr=err,
        )
        # честный сбой (нет токена) в stderr, процесс штатно завершился
        self.assertIn("сбой цикла", err.getvalue())
        self.assertTrue(out.getvalue())
        self.assertEqual(self._demo_rows().count(), 0)
        self.assertEqual(
            Share.objects.get(
                figi=SHARE_DICT["figi"]
            ).price_observations.count(),
            0,
        )

    def test_invalid_interval_fails_with_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("collect_prices", interval=0, source="demo")


class HistorySummaryTests(TestCase):
    """Расчёты изменения: абсолют/процент/особые случаи (F3.4)."""

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT)

    def test_abs_and_pct_change(self):
        from dashboard.services.price_history import summarize

        base = timezone.now() - timedelta(days=30)
        self.share.price_observations.create(
            price=Decimal("100"),
            currency="RUB",
            source="demo",
            quote_time=base,
        )
        self.share.price_observations.create(
            price=Decimal("110"),
            currency="RUB",
            source="demo",
            quote_time=base + timedelta(days=1),
        )
        summary = summarize(
            self.share.price_observations.order_by("quote_time")
        )
        self.assertEqual(summary["change_abs"], Decimal("10"))
        self.assertEqual(summary["change_pct"], Decimal("10.0000"))

    def test_out_of_order_inserts_sorted_by_quote_time(self):
        from dashboard.services.price_history import summarize

        base = timezone.now() - timedelta(days=30)
        self.share.price_observations.create(
            price=Decimal("110"),
            currency="RUB",
            source="demo",
            quote_time=base + timedelta(days=1),
        )
        self.share.price_observations.create(
            price=Decimal("100"),
            currency="RUB",
            source="demo",
            quote_time=base,
        )
        summary = summarize(self.share.price_observations)
        self.assertEqual(summary["first"].price_decimal, Decimal("100"))
        self.assertEqual(summary["last"].price_decimal, Decimal("110"))

    def test_single_point_change_undefined(self):
        from dashboard.services.price_history import summarize

        self.share.price_observations.create(
            price=Decimal("100"),
            currency="RUB",
            source="demo",
            quote_time=timezone.now(),
        )
        summary = summarize(self.share.price_observations)
        self.assertEqual(summary["count"], 1)
        self.assertIsNone(summary["change_abs"])
        self.assertIsNone(summary["change_pct"])

    def test_zero_first_price_pct_undefined_abs_defined(self):
        from dashboard.services.price_history import summarize

        base = timezone.now() - timedelta(days=30)
        self.share.price_observations.create(
            price=Decimal("0"),
            currency="RUB",
            source="demo",
            quote_time=base,
        )
        self.share.price_observations.create(
            price=Decimal("50"),
            currency="RUB",
            source="demo",
            quote_time=base + timedelta(days=1),
        )
        summary = summarize(self.share.price_observations)
        self.assertTrue(summary["zero_first"])
        self.assertIsNone(summary["change_pct"])
        self.assertEqual(summary["change_abs"], Decimal("50"))

    def test_period_filter_excludes_older_observations(self):
        from dashboard.services.price_history import (
            observations_for_period,
            validate_period,
        )

        old = timezone.now() - timedelta(days=100)
        fresh = timezone.now() - timedelta(days=1)
        self.share.price_observations.create(
            price=Decimal("1"), currency="RUB", source="demo", quote_time=old
        )
        self.share.price_observations.create(
            price=Decimal("2"), currency="RUB", source="demo", quote_time=fresh
        )
        week = list(observations_for_period(self.share, validate_period("7d")))
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0].price_decimal, Decimal("2"))
        everything = list(observations_for_period(self.share, None))
        self.assertEqual(len(everything), 2)


class HistoryViewTests(TestCase):
    """Доступный из интерфейса экран истории (F3.4, F3.5)."""

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT)
        self.url = lambda period: (
            reverse("dashboard:history", args=[self.share.id])
            + f"?period={period}"
        )

    def _add_observation(self, price, quote_time=None, source="demo"):
        return self.share.price_observations.create(
            price=Decimal(str(price)),
            currency="RUB",
            source=source,
            quote_time=quote_time or timezone.now(),
        )

    def test_unknown_share_404(self):
        response = self.client.get(reverse("dashboard:history", args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_invalid_period_400(self):
        response = self.client.get(self.url("2w"))
        self.assertEqual(response.status_code, 400)

    def test_empty_history_explicit_message(self):
        response = self.client.get(self.url("1d"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "наблюдений нет")
        self.assertNotContains(response, "Изменение (процент)")

    def test_two_points_show_summary_and_graph(self):
        self._add_observation(
            100, quote_time=timezone.now() - timedelta(hours=2)
        )
        self._add_observation(
            110, quote_time=timezone.now() - timedelta(hours=1)
        )
        response = self.client.get(self.url("1d"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "110")
        self.assertContains(response, "10.0000")
        self.assertContains(response, "plotly")
        self.assertContains(response, "demo (демонстрационные данные)")

    def test_demo_source_marks_banner(self):
        self._add_observation(100)
        response = self.client.get(self.url("1d"))
        self.assertContains(response, "Демонстрационные данные")
        self.assertContains(response, "это не живые котировки")

    def test_single_point_no_graph_message(self):
        self._add_observation(100)
        response = self.client.get(self.url("1d"))
        self.assertContains(response, "как минимум два наблюдения (сейчас 1)")

    def test_zero_first_price_note(self):
        self._add_observation(
            0, quote_time=timezone.now() - timedelta(hours=2)
        )
        self._add_observation(
            50, quote_time=timezone.now() - timedelta(hours=1)
        )
        response = self.client.get(self.url("1d"))
        self.assertContains(response, "начальная цена нулевая")
        self.assertContains(response, "50")

    def test_tinkoff_mode_without_token_shows_honest_status(self):
        with self.settings(TINKOFF_TOKEN="", PRICE_SOURCE="tinkoff"):
            response = self.client.get(self.url("1d"))
        self.assertContains(response, "Интеграция с рынком недоступна")

    def test_links_from_list_and_detail_pages(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(
            response, reverse("dashboard:history", args=[self.share.id])
        )
        response = self.client.get(
            reverse("dashboard:detail", args=[self.share.id])
        )
        self.assertContains(
            response, reverse("dashboard:history", args=[self.share.id])
        )


# --------------------------------------------------------------------------
# Исправления независимой проверки этапа 1: серии (fix 1), точность SQL
# (fix 2), происхождение текущей цены (fix 3), идентичность с историей (fix 4)
# --------------------------------------------------------------------------


class SeriesSplitTests(TestCase):
    """Fix 1: несопоставимые серии истории не смешиваются."""

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT)  # валюта usd
        now = timezone.now()
        PriceObservation.objects.create(
            share=self.share,
            price=str(Decimal("100")),
            currency="usd",
            source="tinkoff",
            quote_time=now - timedelta(days=10),
        )
        PriceObservation.objects.create(
            share=self.share,
            price=str(Decimal("110")),
            currency="RUB",
            source="demo",
            quote_time=now - timedelta(days=9),
        )

    def test_demo_and_market_not_merged_into_one_series(self):
        from dashboard.services.price_history import history_series

        series = history_series(self.share, None)
        self.assertEqual(len(series), 2)
        by_key = {(s["source"], s["currency"]): s for s in series}
        self.assertEqual(set(by_key), {("tinkoff", "usd"), ("demo", "RUB")})
        # ложный +10 USD/+10% больше не возникает: каждая серия — одна точка
        tinkoff = by_key[("tinkoff", "usd")]  # одна точка — изменения нет
        demo = by_key[("demo", "RUB")]
        self.assertEqual(tinkoff["summary"]["count"], 1)
        self.assertEqual(demo["summary"]["count"], 1)
        self.assertIsNone(tinkoff["summary"]["change_abs"])
        self.assertIsNone(demo["summary"]["change_abs"])

    def test_same_currency_different_sources_split(self):
        from dashboard.services.price_history import history_series

        PriceObservation.objects.create(
            share=self.share,
            price=str(Decimal("120")),
            currency="usd",
            source="demo",
            quote_time=timezone.now() - timedelta(days=8),
        )
        series = history_series(self.share, None)
        self.assertEqual(len(series), 3)

    def test_period_upper_bounded_by_now(self):
        from dashboard.services.price_history import (
            observations_for_period,
            validate_period,
        )

        # точка «завтра» (некорректное время котировки) не входит в период
        PriceObservation.objects.create(
            share=self.share,
            price=str(Decimal("999")),
            currency="usd",
            source="tinkoff",
            quote_time=timezone.now() + timedelta(days=1),
        )
        for period in ("1d", "7d", "30d", "90d"):
            rows = list(
                observations_for_period(self.share, validate_period(period))
            )
            self.assertFalse(
                any(o.quote_time > timezone.now() for o in rows),
                period,
            )
            self.assertNotIn(Decimal("999"), [o.price_decimal for o in rows])
        # вся история включает столько наблюдений, сколько создано за период…
        # точка из будущего исключена и из «всей» истории
        everything = list(observations_for_period(self.share, None))
        self.assertNotIn(Decimal("999"), [o.price_decimal for o in everything])


class SQLiteDecimalRoundtripTests(TestCase):
    """Fix 2: полный roundtrip Decimal через реальную SQLite БД.

    NUMERIC-представление на SQLite молча округляло
    (1234567.123456789 → ...6790); TEXT-колонка сохраняет значение точно.
    """

    CASES = (
        "1234567.123456789",
        "12345678901.123456789",
        "100",
        "0.000000001",
    )

    def test_full_roundtrip_via_db(self):
        from dashboard.services.price_history import record_observations

        share = Share.objects.create(**SHARE_DICT)
        base = timezone.now() - timedelta(days=1)
        for index, value in enumerate(self.CASES):
            quote_time = base + timedelta(minutes=index)
            created = record_observations(
                {share.figi: (Decimal(value), quote_time)},
                [share],
                "tinkoff",
            )
            self.assertEqual(created, 1)
            obs = share.price_observations.get(quote_time=quote_time)
            # чтение вне операций возвращает точное сохранённое значение
            self.assertEqual(obs.price, value)
            self.assertEqual(obs.price_decimal, Decimal(value))
        # чтение через свежий запрос из БД (не кеш объекта)
        fresh = Share.objects.get(id=share.id).price_observations.order_by(
            "quote_time"
        )
        values = [obs.price for obs in fresh]
        self.assertEqual(values, list(self.CASES))

    def test_repeated_delivery_after_precision_change_is_deduped(self):
        from dashboard.services.price_history import record_observations

        share = Share.objects.create(**SHARE_DICT)
        base = timezone.now() - timedelta(days=1)
        record_observations(
            {share.figi: (Decimal("1234567.123456789"), base)}, [share], "demo"
        )
        created = record_observations(
            {share.figi: (Decimal("1234567.123456789"), base)},
            [share],
            "demo",
        )
        self.assertEqual(created, 0)


class CurrentPriceProvenanceTests(APITestCase):
    """Fix 3: происхождение текущей цены во всех путях."""

    def _login(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            "provenance@example.com", "provenance@example.com", "pw-123456"
        )
        self.client.force_login(user)
        return user

    def _run_task(self, source="demo", prices=None):
        now = timezone.now()
        if prices is None:
            prices = {SHARE_DICT["figi"]: (Decimal("200"), now)}
        with self.settings(PRICE_SOURCE=source):
            with patch(
                "dashboard.tasks.price_history.fetch_prices",
                return_value=prices,
            ):
                result = tasks.update_last_price()
        return result, now

    def test_update_last_price_demo_marks_demo_source(self):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source=None
        )
        # demo-путь: синтетическая цена НЕ должна затирать рынок незаметно
        result, _ = self._run_task(source="demo")
        share.refresh_from_db()
        self.assertEqual(result, 1)
        self.assertEqual(share.last_price_source, "demo")
        # текущее состояние согласовано с историей (демо-наблюдение записано)
        self.assertEqual(share.price_observations.count(), 1)
        self.assertEqual(share.price_observations.get().source, "demo")

    def test_update_last_price_tinkoff_marks_tinkoff_source(self):
        share = Share.objects.create(**SHARE_DICT)
        self._run_task(source="tinkoff")
        share.refresh_from_db()
        self.assertEqual(share.last_price_source, "tinkoff")
        self.assertEqual(share.price_observations.get().source, "tinkoff")

    def test_error_source_keeps_cached_price_and_origin(self):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="tinkoff"
        )
        with patch(
            "dashboard.tasks.price_history.fetch_prices",
            side_effect=RuntimeError("net"),
        ):
            result = tasks.update_last_price()
        self.assertIsNone(result)
        share.refresh_from_db()
        self.assertEqual(share.last_price, 100.0)
        self.assertEqual(share.last_price_source, "tinkoff")

    def test_collector_celery_paths_consistent(self):
        # локальный коллетор тоже обновляет кеш текущей цены и источник
        share = Share.objects.create(**SHARE_DICT, last_price=100.0)
        result, _ = self._run_task(source="demo")
        self.assertEqual(result, 1)
        share.refresh_from_db()
        self.assertEqual(share.last_price, float(Decimal("200")))
        self.assertEqual(share.last_price_source, "demo")

    def test_api_create_with_price_marks_manual(self):
        self._login()
        response = self.client.post(
            "/api/v1/shares/",
            {**SHARE_DICT, "last_price": 123.45},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        share = Share.objects.get(figi=SHARE_DICT["figi"])
        self.assertEqual(share.last_price_source, "manual")
        response = self.client.get("/api/v1/shares/")
        self.assertContains(response, '"last_price_source":"manual"')

    def test_api_patch_price_marks_manual_without_reset_by_regular_update(
        self,
    ):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="tinkoff"
        )
        self._login()
        # PATCH переименования НЕ сбрасывает происхождение цены
        response = self.client.patch(
            f"/api/v1/shares/{share.id}/",
            {"name": "Amazon2"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share.refresh_from_db()
        self.assertEqual(share.last_price_source, "tinkoff")
        # ручная цена признаётся ручной
        response = self.client.patch(
            f"/api/v1/shares/{share.id}/",
            {"last_price": 55.0},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share.refresh_from_db()
        self.assertEqual(share.last_price_source, "manual")
        # очистка цены снимает происхождение в неизвестные
        response = self.client.patch(
            f"/api/v1/shares/{share.id}/",
            {"last_price": None},
            format="json",
        )
        share.refresh_from_db()
        self.assertIsNone(share.last_price_source)

    def test_list_page_marks_unknown_and_demo_price_origin(self):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="demo"
        )
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, "(demo)")
        share.last_price_source = None
        share.save()
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, "источник неизвестен")

    def test_detail_page_marks_demo_and_honesty(self):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="demo"
        )
        response = self.client.get(
            reverse("dashboard:detail", args=[share.id])
        )
        self.assertContains(response, "demo — демонстрационные данные")
        self.assertNotContains(response, "источник неизвестен")


class IdentityProtectionTests(APITestCase):
    """Fix 4: FIGI/UID/тикер/валюта акции с историей не подменяются."""

    def _login(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            "guard@example.com", "guard@example.com", "pw-123456"
        )
        self.client.force_login(user)
        return user

    def setUp(self):
        self.share = Share.objects.create(**SHARE_DICT)
        self.share.price_observations.create(
            price=str(Decimal("100")),
            currency="usd",
            source="demo",
            quote_time=timezone.now(),
        )
        self._login()

    def test_api_patch_figi_rejected(self):
        response = self.client.patch(
            f"/api/v1/shares/{self.share.id}/",
            {"figi": "BBGNEW0001"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, SHARE_DICT["figi"])
        self.assertEqual(self.share.price_observations.count(), 1)
        self.assertEqual(
            self.share.price_observations.get().share_id, self.share.id
        )

    def test_api_patch_uid_and_ticker_rejected(self):
        response = self.client.patch(
            f"/api/v1/shares/{self.share.id}/",
            {"uid": "uid-new", "ticker": "NEWTK"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.share.refresh_from_db()
        self.assertEqual(self.share.uid, SHARE_DICT["uid"])
        self.assertEqual(self.share.ticker, SHARE_DICT["ticker"])

    def test_api_patch_currency_rejected_with_history(self):
        response = self.client.patch(
            f"/api/v1/shares/{self.share.id}/",
            {"currency": "RUB"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.share.refresh_from_db()
        self.assertEqual(self.share.currency, "usd")

    def test_api_rename_still_allowed(self):
        response = self.client.patch(
            f"/api/v1/shares/{self.share.id}/",
            {"name": "Amazon rebranded"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Share.objects.get(id=self.share.id).name, "Amazon rebranded"
        )

    def test_api_identity_changes_allowed_without_history(self):
        share = Share.objects.create(
            **{
                **SHARE_DICT,
                "figi": "BBG000BLN4K3",
                "uid": "uid-no-history-3",
                "ticker": "AMZN3",
            }
        )
        response = self.client.patch(
            f"/api/v1/shares/{share.id}/",
            {"figi": "BBGNEW0002", "uid": "uid-new-2", "ticker": "NEWTK2"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share.refresh_from_db()
        self.assertEqual(share.figi, "BBGNEW0002")

    def test_html_form_identity_change_rejected_rename_allowed(self):
        url = reverse("dashboard:update", args=[self.share.id])
        response = self.client.post(
            url,
            {
                **SHARE_DICT,
                "figi": "BBGHTMLNEW",
                "uid": "uid-html-new",
                "name": "Amazon2-valid",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.share.refresh_from_db()
        self.assertEqual(self.share.figi, SHARE_DICT["figi"])
        # переименование без смены идентичности работает
        response = self.client.post(
            url,
            {**SHARE_DICT, "name": "Amazon2"},
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        self.assertEqual(Share.objects.get(id=self.share.id).name, "Amazon2")

    def test_admin_identity_change_rejected(self):
        share_admin = ShareAdmin(Share, None)
        form = ShareAdminForm(
            data={
                "figi": "BBGADMINNEW",
                "isin": SHARE_DICT["isin"],
                "name": SHARE_DICT["name"],
                "ticker": SHARE_DICT["ticker"],
                "currency": SHARE_DICT["currency"],
                "uid": SHARE_DICT["uid"],
            },
            instance=self.share,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("figi", form.errors)

    def test_import_skips_protected_identity_fields(self):
        from types import SimpleNamespace

        updated_instrument = SimpleNamespace(
            figi=SHARE_DICT["figi"],
            name="Amazon import",
            isin=SHARE_DICT["isin"],
            ticker="IMPORTTK",
            currency="RUB",
            uid="uid-import",
        )
        response_namespace = SimpleNamespace(instruments=[updated_instrument])
        share_with_history = self.share
        client = MagicMock()
        client.instruments.shares.return_value = response_namespace
        with patch(
            "dashboard.management.commands.add_shares.open_tinkoff_client",
            return_value=_CtxManager(None, client),
        ):
            out, err = StringIO(), StringIO()
            call_command("add_shares", stdout=out, stderr=err)
        self.assertIn("пропуск защищённых полей", out.getvalue())
        share_with_history.refresh_from_db()
        self.assertEqual(share_with_history.ticker, SHARE_DICT["ticker"])
        self.assertEqual(share_with_history.uid, SHARE_DICT["uid"])
        self.assertEqual(share_with_history.currency, "usd")
        self.assertEqual(share_with_history.name, "Amazon import")


# --------------------------------------------------------------------------
# Уточнения после повторной независимой проверки этапа 1: сервер определяет
# источник ручной цены (HTML), очистка цены снимает источник везде, нулевая
# цена имеет пометку, атомарность apply_prices
# --------------------------------------------------------------------------


class ManualPriceSourceHtmlTests(TestCase):
    """Источник ручной цены в HTML определяет сервер, а не форма."""

    def setUp(self):
        user = get_user_model().objects.create_user(
            "html@example.com", "html@example.com", "pw-123456"
        )
        self.client.force_login(user)
        self.share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="demo"
        )
        self.update_url = reverse("dashboard:update", args=[self.share.id])
        self.create_url = reverse("dashboard:create")

    def _post_update(self, **extra):
        data = {
            key: self.share.__dict__[key]
            for key in ("figi", "isin", "name", "ticker", "currency", "uid")
        }
        data.update(extra)
        response = self.client.post(self.update_url, data)
        self.share.refresh_from_db()
        return response

    def test_spoofed_cannot_mark_manual_price_as_market(self):
        # цена изменена на 200, а из формы передан источник tinkoff —
        # источник определяет сервер: изменённая цена становится manual
        response = self._post_update(
            last_price="200", last_price_source="tinkoff"
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        self.assertEqual(self.share.last_price, 200.0)
        self.assertEqual(self.share.last_price_source, "manual")

    def test_rename_without_price_change_keeps_original_source(self):
        # обычное переименование: цена и источник не затрагиваются
        response = self._post_update(name="Amazon2", last_price="100.0")
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        self.assertEqual(self.share.name, "Amazon2")
        self.assertEqual(self.share.last_price_source, "demo")

    def test_cleared_price_removes_source(self):
        response = self._post_update(
            last_price="", last_price_source="tinkoff"
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        self.assertIsNone(self.share.last_price)
        self.assertIsNone(self.share.last_price_source)

    def test_create_with_price_marks_manual_ignoring_spoof(self):
        # создание через HTML: цена есть — источник manual, к spoof-полю
        # из POST форма не даёт доступа
        data = {
            **SHARE_DICT,
            "figi": "BBG000BLN4K9",
            "uid": "uid-create-9",
            "ticker": "AMZN9",
            "last_price": "150",
            "last_price_source": "demo",
        }
        response = self.client.post(self.create_url, data)
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        created = Share.objects.get(figi="BBG000BLN4K9")
        self.assertEqual(created.last_price_source, "manual")
        created.delete()


class PriceProvenanceConsistencyTests(APITestCase):
    """Очистка/нулевая цена — согласованно в HTML, admin и API."""

    def _login(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            "consistency@example.com", "consistency@example.com", "pw-123456"
        )
        self.client.force_login(user)
        return user

    def _admin_form(self, data, share):
        return ShareAdminForm(data=data, instance=share)

    def test_api_create_zero_price_is_manual(self):
        self._login()
        response = self.client.post(
            "/api/v1/shares/",
            {**SHARE_DICT, "last_price": 0.0, "last_price_source": "tinkoff"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        share = Share.objects.get(id=response.data["id"])
        self.assertEqual(share.last_price, 0.0)
        self.assertEqual(share.last_price_source, "manual")
        share.delete()

    def test_api_create_null_price_has_no_source(self):
        self._login()
        response = self.client.post(
            "/api/v1/shares/",
            {**SHARE_DICT, "last_price": None, "last_price_source": "tinkoff"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        share = Share.objects.get(id=response.data["id"])
        self.assertIsNone(share.last_price)
        self.assertIsNone(share.last_price_source)
        share.delete()

    def test_admin_clearing_price_removes_source(self):
        share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="demo"
        )
        admin_data = {
            **SHARE_DICT,
            "last_price": "",
            "is_trend_high": "",
        }
        form = self._admin_form(admin_data, share)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        share.refresh_from_db()
        self.assertIsNone(share.last_price)
        self.assertIsNone(share.last_price_source)

    def test_admin_manual_price_zero_is_manual_even_with_spoof(self):
        share = Share.objects.create(**SHARE_DICT)
        admin_data = {
            **SHARE_DICT,
            "last_price": "0",
            "last_price_source": "tinkoff",
            "is_trend_high": "",
        }
        form = self._admin_form(admin_data, share)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        share.refresh_from_db()
        self.assertEqual(share.last_price, 0.0)
        self.assertEqual(share.last_price_source, "manual")


class ZeroPriceDisplayTests(TestCase):
    """Нулевая цена имеет пометку источника (проверка значения, не истинности)."""

    def setUp(self):
        self.share = Share.objects.create(
            **SHARE_DICT, last_price=0.0, last_price_source="demo"
        )

    def test_detail_shows_demo_marker_for_zero_price(self):
        response = self.client.get(
            reverse("dashboard:detail", args=[self.share.id])
        )
        self.assertContains(response, "0.0")
        self.assertContains(response, "demo — демонстрационные данные")

    def test_list_shows_demo_marker_for_zero_price(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, "(demo)")


class ApplyPricesAtomicityTests(TestCase):
    """Атомарность кеша и наблюдения на инструмент (fail-rollback)."""

    def setUp(self):
        self.share = Share.objects.create(
            **SHARE_DICT, last_price=100.0, last_price_source="demo"
        )

    def test_failed_observation_write_rolls_back_cache(self):
        from django.db import DatabaseError

        from dashboard.services import price_history as ph

        now = timezone.now()
        prices = {SHARE_DICT["figi"]: (Decimal("777"), now)}
        with patch(
            "dashboard.services.price_history.record_observations",
            side_effect=DatabaseError("db down"),
        ):
            with self.assertRaises(DatabaseError):
                ph.apply_prices(prices, [self.share], "tinkoff", now=now)
        self.share.refresh_from_db()
        # кеш остался прежним: цена/источник не «обновлены» без истории
        self.assertEqual(self.share.last_price, 100.0)
        self.assertEqual(self.share.last_price_source, "demo")
        self.assertEqual(self.share.price_observations.count(), 0)

    def test_successful_apply_writes_cache_and_observation(self):
        from dashboard.services import price_history as ph

        now = timezone.now()
        updated = ph.apply_prices(
            {SHARE_DICT["figi"]: (Decimal("777.5"), now)},
            [self.share],
            "tinkoff",
            now=now,
        )
        self.assertEqual(updated, 1)
        self.share.refresh_from_db()
        self.assertEqual(self.share.last_price, 777.5)
        self.assertEqual(self.share.last_price_source, "tinkoff")
        observation = self.share.price_observations.get()
        self.assertEqual(observation.price_decimal, Decimal("777.5"))
        observation.delete()
