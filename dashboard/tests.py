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
        processed, created = ph.apply_prices(
            {SHARE_DICT["figi"]: (Decimal("777.5"), now)},
            [self.share],
            "tinkoff",
            now=now,
        )
        self.assertEqual(processed, 1)
        self.assertEqual(created, 1)
        self.share.refresh_from_db()
        self.assertEqual(self.share.last_price, 777.5)
        self.assertEqual(self.share.last_price_source, "tinkoff")
        observation = self.share.price_observations.get()
        self.assertEqual(observation.price_decimal, Decimal("777.5"))
        observation.delete()


# --- Этап 2: виртуальные портфели ------------------------------------------

from django.conf import settings as dj_settings  # noqa: E402

from dashboard.models import Portfolio, Trade  # noqa: E402
from dashboard.services.portfolio_accounting import (  # noqa: E402
    canonical_text_for_stored,
    compute_positions,
    display_round,
    execute_trade,
    last_suitable_observation,
    portfolio_summary,
    position_rows,
    validate_executed_time,
    validate_metric,
)

settings = dj_settings


class PortfolioBase(TestCase):
    """Общая подготовка для тестов виртуальных портфелей."""

    USER_EMAIL = "writer@example.com"
    USER_PASSWORD = "N00c2_f_k_correct"

    def setUp(self):
        from dashboard.models import Portfolio

        self.user = get_user_model().objects.create_user(
            self.USER_EMAIL, self.USER_EMAIL, self.USER_PASSWORD
        )
        self.other_user = get_user_model().objects.create_user(
            "other@example.com", "other@example.com", "N00c2_f_k_correct"
        )
        self.share = Share.objects.create(
            figi="BBG000B9XRY4",
            isin="US0378331005",
            name="Apple",
            ticker="AAPL",
            currency="USD",
            uid="a9eb4238-eba9-488c-b102-b6140fd08e38",
        )
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.sqlite3",
        )

    def make_portfolio(
        self,
        user=None,
        name="Main",
        currency="USD",
        source="demo",
        initial="1000",
    ):
        from dashboard.models import Portfolio

        return Portfolio.objects.create(
            user=user or self.user,
            name=name,
            base_currency=currency,
            valuation_source=source,
            initial_cash=initial,
            cash=initial,
        )

    def trade(
        self,
        portfolio,
        side,
        quantity,
        price,
        commission="0",
        minutes=-10,
        share=None,
        note="",
        idempotency_key=None,
        at=None,
    ):
        from django.utils import timezone

        if at is None:
            at = timezone.now() + timedelta(minutes=minutes)
        return execute_trade(
            portfolio,
            side=side,
            share=share or self.share,
            quantity=quantity,
            price=price,
            commission=commission,
            executed_at=at,
            note=note,
            idempotency_key=idempotency_key,
        )


from django.conf import settings as dj_settings  # noqa: E402

settings = dj_settings


class ControlExampleTests(PortfolioBase):
    """Контрольный пример из задания (критерий 1)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio()
        self.trade(self.p, "BUY", "2", "100", commission="2", minutes=-30)
        self.trade(self.p, "BUY", "3", "120", commission="3", minutes=-20)
        self.trade(self.p, "SELL", "2", "130", commission="2", minutes=-10)

    def positions(self):
        from dashboard.services.portfolio_accounting import compute_positions

        return compute_positions(self.p)[self.share.figi]

    def test_quantity_and_avg_cost(self):
        pos = self.positions()
        self.assertEqual(pos["open_qty"], Decimal("3"))
        self.assertEqual(pos["avg_cost"], Decimal("113"))

    def test_cash(self):
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("693"))

    def test_realized_pl(self):
        self.assertEqual(self.positions()["realized_pl"], Decimal("32"))

    def test_valuation_at_125(self):
        from dashboard.models import PriceObservation

        quote_time = timezone.now()
        PriceObservation.objects.create(
            share=self.share,
            price="125",
            currency="USD",
            source="demo",
            quote_time=quote_time,
        )
        obs = last_suitable_observation(
            self.share, "demo", "USD", as_of=quote_time + timedelta(seconds=1)
        )
        self.assertEqual(obs.price_decimal, Decimal("125"))
        self.p.refresh_from_db()
        summary = portfolio_summary(
            self.p, as_of=quote_time + timedelta(seconds=1)
        )
        self.assertEqual(summary["unrealized_pl"], Decimal("36"))
        self.assertEqual(summary["overall_pl"], Decimal("68"))
        self.assertEqual(summary["total_value"], Decimal("1068"))
        self.assertEqual(summary["return_pct"].normalize(), Decimal("6.8"))
        self.assertFalse(summary["incomplete"])


class AveragingAndClosingTests(PortfolioBase):
    """Средняя себестоимость, частичная продажа, полное закрытие."""

    def test_two_buys_weighted_average(self):
        p = self.make_portfolio(initial="10000")
        self.trade(p, "BUY", "10", "100", minutes=-20)
        self.trade(p, "BUY", "10", "110", minutes=-10)
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("20"))
        self.assertEqual(pos["avg_cost"], Decimal("105"))

    def test_partial_sell_keeps_average(self):
        p = self.make_portfolio(initial="10000")
        self.trade(p, "BUY", "10", "100", minutes=-20)
        res = self.trade(p, "SELL", "4", "90", commission="1", minutes=-5)
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("6"))
        self.assertEqual(pos["avg_cost"], Decimal("100"))
        # реализованный P/L: 4*90 - 1 - 4*100 = -41
        self.assertEqual(pos["realized_pl"], Decimal("-41"))

    def test_full_close_then_new_buy(self):
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "5", "100", commission="0", minutes=-20)
        self.trade(p, "SELL", "5", "110", commission="0", minutes=-10)
        positions = compute_positions(p)
        self.assertEqual(positions[self.share.figi]["open_qty"], Decimal("0"))
        self.assertIsNone(positions[self.share.figi]["avg_cost"])
        # новая покупка начинает расчёт с нуля
        self.trade(p, "BUY", "2", "120", commission="0", minutes=-5)
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("2"))
        self.assertEqual(pos["avg_cost"], Decimal("120"))


class RejectionTests(PortfolioBase):
    """Отказы без частичных изменений (F2.4)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio(initial="100")
        self.p.refresh_from_db()

    def test_sell_more_than_owned(self):
        self.trade(self.p, "BUY", "5", "10", minutes=-20)
        with self.assertRaises(ValueError):
            self.trade(self.p, "SELL", "6", "10", minutes=-10)
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("50"))

    def test_insufficient_funds(self):
        with self.assertRaises(ValueError):
            self.trade(self.p, "BUY", "20", "10", commission="1", minutes=-20)
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("100"))
        self.assertEqual(self.p.trades.count(), 0)

    def test_currency_mismatch(self):
        other = Share.objects.create(
            figi="BBG00EUR1",
            isin="EUR0001",
            name="Euro stock",
            ticker="EUR1",
            currency="EUR",
            uid="uid-eur-1",
        )
        with self.assertRaises(ValueError):
            self.trade(self.p, "BUY", "1", "1", minutes=-20, share=other)
        self.assertEqual(self.p.trades.count(), 0)

    def test_bad_values(self):
        for kwargs in (
            {"quantity": "0", "price": "1"},
            {"quantity": "-1", "price": "1"},
            {"quantity": "1", "price": "0"},
            {"quantity": "1", "price": "-5"},
            {"quantity": "1", "price": "1", "commission": "-0.5"},
            {"quantity": "abc", "price": "1"},
            {"quantity": "NaN", "price": "1"},
            {"quantity": "Infinity", "price": "1"},
        ):
            with self.assertRaises(ValueError):
                self.trade(
                    self.p,
                    "BUY",
                    kwargs["quantity"],
                    kwargs["price"],
                    commission=kwargs.get("commission", "0"),
                    minutes=-20,
                )
        self.assertEqual(self.p.trades.count(), 0)

    def test_future_time_rejected(self):
        with self.assertRaises(ValueError):
            self.trade(self.p, "BUY", "1", "1", minutes=10)
        self.assertEqual(self.p.trades.count(), 0)

    def test_time_earlier_than_last_rejected(self):
        self.trade(self.p, "BUY", "1", "1", minutes=-10)
        self.p.refresh_from_db()
        with self.assertRaises(ValueError):
            self.trade(self.p, "BUY", "1", "1", minutes=-20)
        self.assertEqual(self.p.trades.count(), 1)

    def test_equal_time_allowed_with_stable_order(self):
        """Одинаковые времена: операции идут в устойчивом порядке (id)."""
        at = timezone.now() - timedelta(minutes=10)
        from dashboard.services.portfolio_accounting import execute_trade

        t1 = execute_trade(
            self.p,
            side="BUY",
            share=self.share,
            quantity="1",
            price="10",
            executed_at=at,
        )
        t2 = execute_trade(
            self.p,
            side="SELL",
            share=self.share,
            quantity="1",
            price="10",
            executed_at=at,
        )
        names = [t.pk for t in self.p.trades.order_by("executed_at", "id")]
        self.assertEqual(names, [t1.pk, t2.pk])


class IdempotencyAndAtomicityTests(PortfolioBase):
    """Идемпотентность ключа и атомарность отказа (F2.8)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio(initial="1000")

    def test_same_key_same_data_no_duplicate(self):
        shared_at = timezone.now() - timedelta(minutes=10)
        t1 = self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op-1",
            at=shared_at,
        )
        self.p.refresh_from_db()
        cash_after = self.p.cash_decimal
        t2 = self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op-1",
            at=shared_at,
        )
        self.assertEqual(t1.pk, t2.pk)
        self.assertEqual(self.p.trades.count(), 1)
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, cash_after)

    def test_same_key_different_data_rejected(self):
        self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            minutes=-10,
            idempotency_key="op-1",
        )
        with self.assertRaises(ValueError):
            self.trade(
                self.p,
                "BUY",
                "3",
                "100",
                commission="2",
                minutes=-9,
                idempotency_key="op-1",
            )
        self.assertEqual(self.p.trades.count(), 1)

    def test_atomic_failure_no_partial_changes(self):
        """Сбой записи операции не оставляет списанных денег."""
        from dashboard.models import Trade
        from dashboard.services.portfolio_accounting import execute_trade

        with patch.object(
            Trade.objects, "create", side_effect=RuntimeError("db failure")
        ):
            at = timezone.now() - timedelta(minutes=5)
            with self.assertRaises(ValueError):
                execute_trade(
                    self.p,
                    side="BUY",
                    share=self.share,
                    quantity="2",
                    price="100",
                    commission="2",
                    executed_at=at,
                )
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("1000"))
        self.assertEqual(self.p.trades.count(), 0)


class ExactRoundtripPortfolioTests(PortfolioBase):
    """Точный roundtrip SQLite (критерий 6).

    Политика точности: значения для хранения — до 30 целых и 18
    дробных цифр (текст ≤ 48 символов); вычисления — в широком
    контексте (120 цифр), накопленный cash — точный.
    """

    def test_many_digit_values_roundtrip(self):
        initial = "23456789012345678901234567890.12345678"
        p = self.make_portfolio(initial=initial)
        qty = "987654321098765.4321"
        price = "23.98765432109876"
        comm = "0.000000000000000015"
        self.trade(p, "BUY", qty, price, commission=comm, minutes=-10)
        p.refresh_from_db()
        tr = p.trades.get()
        # значения журнала — канонические тексты введённых величин
        self.assertEqual(tr.quantity, qty)
        self.assertEqual(tr.price, price)
        self.assertEqual(tr.commission, comm)
        # накопленный cash — точный результат.
        # Эталон считается в независимом контексте (80 цифр, покрывает
        # все длины этого примера) и дополняется литеральной сверкой
        # существенных разрядов, чтобы потерю округления было видно.
        from decimal import localcontext

        with localcontext() as ctx:
            ctx.prec = 80
            expected = (
                Decimal(initial)
                - Decimal(qty) * Decimal(price)
                - Decimal(comm)
            )
        self.assertEqual(p.cash_decimal, expected)
        self.assertEqual(str(p.cash_decimal), str(expected))
        reread = type(p).objects.get(id=p.id)
        self.assertEqual(str(reread.cash), str(p.cash))


class JournalAndValuationTests(PortfolioBase):
    """Журнал, пагинация, оценка, снимок идентичности (F2.6/F2.7)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio(initial="10000")

    def _mk_obs(
        self, price, quote_time=None, source="demo", currency="USD", share=None
    ):
        return PriceObservation.objects.create(
            share=share or self.share,
            price=price,
            currency=currency,
            source=source,
            quote_time=quote_time or timezone.now(),
        )

    def test_journal_pagination(self):
        for i in range(25):
            self.trade(self.p, "BUY", "1", "1", minutes=-(1000 - i))
        self.client.force_login(self.user)
        url = reverse("dashboard:portfolio-journal", args=[self.p.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        # первая страница: 20 записей
        count = r.context["page_obj"].object_list.count()
        self.assertEqual(count, 20)
        r2 = self.client.get(url + "?page=2")
        self.assertEqual(r2.context["page_obj"].object_list.count(), 5)

    def test_missing_quote_marks_incomplete(self):
        self.trade(self.p, "BUY", "2", "10", minutes=-10)
        # наблюдений нет вовсе
        self.p.refresh_from_db()
        summary = portfolio_summary(self.p)
        self.assertTrue(summary["incomplete"])
        self.assertEqual(summary["market_value"], Decimal("0"))
        self.assertEqual(summary["cash"], Decimal("9980"))
        self.assertIsNone(summary["unrealized_pl"])

    def test_valuation_source_and_currency_matching_case_insensitive(self):
        """USD/usd — одна валюта; источник не смешивается."""
        self.trade(self.p, "BUY", "1", "10", minutes=-10)
        self.p.refresh_from_db()
        self._mk_obs("42.5", source="demo", currency="usd")
        summary = portfolio_summary(self.p)
        self.assertFalse(summary["incomplete"])
        self.assertEqual(summary["market_value"], Decimal("42.5"))

    def test_other_source_quote_not_used(self):
        """Котировка другого источника не подменяет выбранную оценку."""
        self.trade(self.p, "BUY", "1", "10", minutes=-10)
        self.p.refresh_from_db()
        # tinkoff-наблюдение есть, demo (выбранный источник) — нет
        self._mk_obs("999", source="tinkoff")
        summary = portfolio_summary(self.p)
        self.assertTrue(summary["incomplete"])

    def test_future_quote_excluded(self):
        self.trade(self.p, "BUY", "1", "10", minutes=-10)
        self.p.refresh_from_db()
        self._mk_obs("999", quote_time=timezone.now() + timedelta(days=1))
        summary = portfolio_summary(self.p)
        self.assertTrue(summary["incomplete"])

    def test_quote_choice_is_deterministic(self):
        """Выбор котировки детерминирован и устойчив при повторах.

        Дедупликация (share, source, quote_time) не допускает двух
        наблюдений одного инструмента/источника с равным quote_time;
        tie-breakers (received_at, id) остаются для устойчивости.
        """
        self.trade(self.p, "BUY", "1", "10", minutes=-10)
        self.p.refresh_from_db()
        self._mk_obs("11", quote_time=timezone.now() - timedelta(minutes=2))
        last = self._mk_obs(
            "12", quote_time=timezone.now() - timedelta(seconds=5)
        )
        picked = last_suitable_observation(self.share, "demo", "USD")
        self.assertEqual(picked.price_decimal, Decimal("12"))
        self.assertEqual(picked.pk, last.pk)
        picked_again = last_suitable_observation(self.share, "demo", "USD")
        self.assertEqual(picked.pk, picked_again.pk)
        # старая котировка (quote_time позже) не «всплывает»
        older = self._mk_obs(
            "9", quote_time=timezone.now() - timedelta(days=1)
        )
        self.assertNotEqual(
            last_suitable_observation(self.share, "demo", "USD").pk, older.pk
        )

    def test_demo_source_marked_in_templates(self):
        self.client.force_login(self.user)
        r = self.client.get(
            reverse("dashboard:portfolio-detail", args=[self.p.pk])
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "DEMO")

    def test_trade_snapshot_survives_share_delete(self):
        t = self.trade(
            self.p, "BUY", "1", "10", minutes=-10, note="память инструментов"
        )
        figi, ticker, name, cur = (
            t.figi,
            t.ticker,
            t.share_name,
            t.share_currency,
        )
        self.share.delete()
        t.refresh_from_db()
        self.assertIsNone(t.share)
        self.assertEqual(
            (t.figi, t.ticker, t.share_name, t.share_currency),
            (figi, ticker, name, cur),
        )
        # журнал доступен без 500
        self.client.force_login(self.user)
        r = self.client.get(
            reverse("dashboard:portfolio-journal", args=[self.p.pk])
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "AAPL")

    def test_portfolio_delete_with_trades_protected(self):
        self.trade(self.p, "BUY", "1", "10", minutes=-10)
        self.client.force_login(self.user)
        r = self.client.post(
            reverse("dashboard:portfolio-delete", args=[self.p.pk])
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "нельзя удалить")
        # журнал сохранился
        self.assertEqual(self.p.trades.count(), 1)

    def test_portfolio_delete_without_trades_ok(self):
        p = self.make_portfolio(name="Empty", initial="10")
        self.client.force_login(self.user)
        r = self.client.post(
            reverse("dashboard:portfolio-delete", args=[p.pk])
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Portfolio.objects.filter(pk=p.pk).exists())


class OwnershipAndSecurityTests(PortfolioBase):
    """Владелец на сервере, CSRF, доступ (F2.9)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio(initial="100")
        self.detail_url = reverse(
            "dashboard:portfolio-detail", args=[self.p.pk]
        )
        self.journal_url = reverse(
            "dashboard:portfolio-journal", args=[self.p.pk]
        )
        self.trade_url = reverse(
            "dashboard:portfolio-trade-new", args=[self.p.pk]
        )

    def test_anonymous_redirects_to_login(self):
        for url in (self.detail_url, self.journal_url, self.trade_url):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)

    def test_foreign_portfolio_404(self):
        self.client.force_login(self.other_user)
        for url in (self.detail_url, self.journal_url, self.trade_url):
            self.assertEqual(self.client.get(url).status_code, 404)
        delete_url = reverse("dashboard:portfolio-delete", args=[self.p.pk])
        # POST — только на действующие POST-эндпоинты
        for url in (self.trade_url, delete_url):
            self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_post_requires_csrf(self):
        """POST-форма без CSRF-токена отклоняется (403)."""
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        r = client.post(self.trade_url, {"side": "BUY"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.p.trades.count(), 0)

    def test_full_http_operation_flow(self):
        """Живой scenario через клиент: создание портфеля, 2 покупки,
        продажа, журнал (для HTTP-прогона есть отдельная проверка на
        реальном сервере; здесь — интеграционная)."""
        self.client.force_login(self.user)
        # создание портфеля
        r = self.client.post(
            reverse("dashboard:portfolio-create"),
            {
                "name": "HTTP Flow",
                "base_currency": "USD",
                "valuation_source": "demo",
                "initial_cash": "1000",
            },
        )
        self.assertEqual(r.status_code, 302)
        portfolio = Portfolio.objects.get(user=self.user, name="HTTP Flow")
        self.assertEqual(portfolio.cash, "1000")

        at = timezone.now().strftime("%Y-%m-%d %H:%M")

        def post_trade(qty, price, side, commission):
            return self.client.post(
                reverse("dashboard:portfolio-trade-new", args=[portfolio.pk]),
                {
                    "share": str(self.share.id),
                    "side": side,
                    "quantity": qty,
                    "price": price,
                    "commission": commission,
                    "executed_at": at,
                    "note": "",
                },
            )

        r = post_trade("2", "100", "BUY", "2")
        self.assertEqual(r.status_code, 302)
        r = post_trade("3", "120", "BUY", "3")
        self.assertEqual(r.status_code, 302)
        r = post_trade("2", "130", "SELL", "2")
        self.assertEqual(r.status_code, 302)
        portfolio.refresh_from_db()
        self.assertEqual(portfolio.cash_decimal, Decimal("693"))
        # журнал отображает обе покупки и продажу
        r = self.client.get(
            reverse("dashboard:portfolio-journal", args=[portfolio.pk])
        )
        self.assertContains(r, "AAPL")

    def test_trade_post_invalid_value_is_rejected(self):
        self.client.force_login(self.user)
        at = timezone.now().strftime("%Y-%m-%d %H:%M")
        r = self.client.post(
            self.trade_url,
            {
                "share": str(self.share.id),
                "side": "BUY",
                "quantity": "999",  # денег не хватит
                "price": "100",
                "commission": "0",
                "executed_at": at,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "недостаточно")
        self.assertEqual(self.p.trades.count(), 0)
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("100"))

    def test_form_prefills_price_from_observation(self):
        self._mk_obs_unused = PriceObservation.objects.create(
            share=self.share,
            price="123.45",
            currency="USD",
            source="demo",
            quote_time=timezone.now() - timedelta(minutes=5),
        )
        self.client.force_login(self.user)
        r = self.client.get(self.trade_url + f"?share={self.share.id}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "123.45")
        self.assertContains(r, "Demo")
        self.assertContains(r, "DEMO")


class HasPriceHistoryTradesTests(PortfolioBase):
    """Идентичность акции с операциями защищена (расширение has_price_history)."""

    def test_share_with_trades_protected_identity(self):
        p = self.make_portfolio(initial="100")
        self.trade(p, "BUY", "1", "10", minutes=-10)
        self.share.refresh_from_db()
        self.assertTrue(self.share.has_price_history())
        changed = self.share.protected_identity_changes(
            {
                "figi": "NEW-FIGI",
                "uid": "new-uid",
                "ticker": "NEWTKR",
                "currency": "EUR",
            }
        )
        self.assertEqual(
            sorted(changed), ["currency", "figi", "ticker", "uid"]
        )


class ValidateMetricUnitTests(TestCase):
    """Граничные значения валидации величин."""

    def test_zero_quantity_rejected_zero_commission_ok(self):
        from dashboard.services.portfolio_accounting import validate_metric

        with self.assertRaises(ValueError):
            validate_metric("0", positive=True, label="количество")
        validate_metric("0", positive=False, label="комиссия")
        with self.assertRaises(ValueError):
            validate_metric("-0.0", positive=True, label="количество")
        with self.assertRaises(ValueError):
            validate_metric("1E+309", positive=True, label="цена")
        self.assertEqual(
            validate_metric("0.00000981", positive=True, label="количество"),
            Decimal("0.00000981"),
        )
        with self.assertRaises(ValueError):
            validate_metric(None, positive=True, label="количество")

    def test_naive_datetime_treated_as_utc(self):
        import datetime as dt

        from dashboard.services.portfolio_accounting import (
            validate_executed_time,
        )

        naive = (timezone.now() - timedelta(minutes=5)).replace(tzinfo=None)
        t = validate_executed_time(naive)
        self.assertTrue(timezone.is_aware(t))
        future = timezone.now() + timedelta(minutes=5)
        with self.assertRaises(ValueError):
            validate_executed_time(future)


# --- Дополнительные регрессии ревизии этапа 2 ------------------------------


class IdempotentRetryAfterLaterOpsTests(PortfolioBase):
    """Повтор старой операции валиден и после новых (дефект ревизии 1)."""

    def setUp(self):
        super().setUp()
        self.p = self.make_portfolio(initial="1000")

    def test_retry_returns_old_trade_even_after_new_ops(self):
        t1_at = timezone.now() - timedelta(minutes=30)
        t2_at = timezone.now() - timedelta(minutes=10)
        t1 = self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op1",
            at=t1_at,
        )
        self.p.refresh_from_db()
        cash_after = self.p.cash_decimal
        self.trade(
            self.p,
            "BUY",
            "1",
            "50",
            commission="1",
            idempotency_key="op2",
            at=t2_at,
        )
        self.p.refresh_from_db()
        cash_before_retry = self.p.cash_decimal
        # точный повтор op1 (прежние данные и прежний ключ t1)
        t1_retry = self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op1",
            at=t1_at,
        )
        self.assertEqual(t1_retry.pk, t1.pk)
        self.p.refresh_from_db()
        # cash не изменился (не списал дважды и не вернул), журнал тот же
        self.assertEqual(self.p.cash_decimal, cash_before_retry)
        self.assertEqual(self.p.trades.count(), 2)
        ordered = list(
            self.p.trades.order_by("executed_at", "id").values_list(
                "id", flat=True
            )
        )
        self.assertEqual(len(ordered), 2)
        self.assertEqual(ordered[0], t1.pk)  # повтор не создал дубль

    def test_new_key_backdated_still_rejected(self):
        self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op1",
            at=timezone.now() - timedelta(minutes=30),
        )
        self.trade(
            self.p,
            "BUY",
            "1",
            "50",
            commission="1",
            idempotency_key="op2",
            at=timezone.now() - timedelta(minutes=10),
        )
        self.p.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            self.trade(
                self.p,
                "BUY",
                "1",
                "1",
                commission="0",
                idempotency_key="op3",
                at=timezone.now() - timedelta(minutes=25),
            )
        self.assertIn("раньше", str(ctx.exception))
        self.p.refresh_from_db()
        self.assertEqual(self.p.cash_decimal, Decimal("747"))
        self.assertEqual(self.p.trades.count(), 2)

    def test_same_key_other_data_still_rejected(self):
        self.trade(
            self.p,
            "BUY",
            "2",
            "100",
            commission="2",
            idempotency_key="op1",
            at=timezone.now() - timedelta(minutes=30),
        )
        with self.assertRaises(ValueError):
            self.trade(
                self.p,
                "BUY",
                "2",
                "100",
                commission="3",
                idempotency_key="op1",
                at=timezone.now() - timedelta(minutes=30),
            )


class LeadingZeroCashTests(PortfolioBase):
    """Ведущие нули в начальном балансе не блокируют сделки (дефект 2)."""

    def test_form_canonicalizes_initial_cash(self):
        self.client.force_login(self.user)
        r = self.client.post(
            reverse("dashboard:portfolio-create"),
            {
                "name": "Zeros",
                "base_currency": "USD",
                "valuation_source": "demo",
                "initial_cash": "001000",
            },
        )
        self.assertEqual(r.status_code, 302)
        p = Portfolio.objects.get(user=self.user, name="Zeros")
        self.assertEqual(p.initial_cash, "1000")
        self.assertEqual(p.cash, "1000")
        # первая покупка проходит, cash = 999
        at = timezone.now().strftime("%Y-%m-%d %H:%M")
        r = self.client.post(
            reverse("dashboard:portfolio-trade-new", args=[p.pk]),
            {
                "share": str(self.share.id),
                "side": "BUY",
                "quantity": "1",
                "price": "1",
                "commission": "0",
                "executed_at": at,
            },
        )
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.cash, "999")

    def test_legacy_noncanonical_cash_still_trades(self):
        # уже сохранённые неканонические, но корректные тексты работают:
        # CAS сравнивается с фактическим текстом в БД
        p = self.make_portfolio(name="Legacy")
        Portfolio.objects.filter(id=p.id).update(cash="001000")
        self.trade(p, "BUY", "1", "1", commission="0", minutes=-10)
        p.refresh_from_db()
        self.assertEqual(p.cash, "999")


class PrecisionPolicyTests(PortfolioBase):
    """Явная политика точности: молчаливо бесплатных сделок нет (дефект 3)."""

    def test_micro_trade_rejected_before_write(self):
        p = self.make_portfolio(initial="1000")
        micro = "0.000000000000000000000001"  # 10^-24
        with self.assertRaises(ValueError) as ctx:
            self.trade(p, "BUY", micro, micro, commission="0", minutes=-10)
        self.assertIn("точност", str(ctx.exception))
        p.refresh_from_db()
        self.assertEqual(p.cash, "1000")
        self.assertEqual(p.trades.count(), 0)

    def test_small_trade_within_policy_is_exact(self):
        # дельта 10^-8 — в пределах заявленной точности (18 дробных цифр)
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "1", "0.00000001", commission="0", minutes=-10)
        p.refresh_from_db()
        self.assertEqual(p.cash_decimal, Decimal("999.99999999"))

    def test_derived_market_value_exact(self):
        """Производные величины точны (дефект 3б): эталон — независимая
        арифметика с достаточной точностью, не тот же контекст, что в
        сервисе. Сценарий — открытая позиция и её ОБЫЧНЫЙ курс;
        стоимость позиции вычисляется из журнала.

        Величина порядка 10^37 провалила бы 50-значный контекст и
        «известную» ошибку 4980948001.249809479 — здесь проверяется,
        что значение соответствует литералу.
        """
        p = self.make_portfolio(initial="1000")
        qty = "12345678901234567890123456789"
        quote = "1234567890.123456789"
        # журнальная запись создаётся напрямую (снапшот) — проверяем
        # именно арифметику производных, не cash-путь
        Trade.objects.create(
            portfolio=p,
            share=self.share,
            figi=self.share.figi,
            ticker=self.share.ticker,
            share_name=self.share.name,
            share_currency=self.share.currency,
            side="BUY",
            quantity=qty,
            price="100",
            commission="0",
            currency="USD",
            executed_at=timezone.now() - timedelta(minutes=10),
            payload_hash="journal-seed",
        )
        PriceObservation.objects.create(
            share=self.share,
            price=quote,
            currency="USD",
            source="demo",
            quote_time=timezone.now(),
        )
        p.refresh_from_db()
        rows = position_rows(p)
        card = rows["cards"][0]
        # точное значение (литерал из задания)
        expected = Decimal("15241578753238836750342935775019051998.750190521")
        self.assertEqual(card["market_value"], expected)
        # эталон независимо умножением в отдельном контексте
        from decimal import localcontext as _lc

        with _lc() as ref:
            ref.prec = 80  # покрывает все длины (47 значащих цифр)
            self.assertEqual(Decimal(qty) * Decimal(quote), expected)
        # и стоимость позиции не повторяет котировку (раньше был дефект)
        self.assertNotEqual(card["market_value"], Decimal(quote))

    def test_position_market_value_column(self):
        """Текущая стоимость позиции = количество × котировка (дефект 4)."""
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "3", "100", commission="0", minutes=-10)
        PriceObservation.objects.create(
            share=self.share,
            price="125",
            currency="USD",
            source="demo",
            quote_time=timezone.now(),
        )
        p.refresh_from_db()
        self.client.force_login(self.user)
        r = self.client.get(reverse("dashboard:portfolio-detail", args=[p.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "125.0000")  # котировка
        self.assertContains(r, "375.0000")  # 3 × 125


class IncompleteTotalsTests(PortfolioBase):
    """Честные итоги при неполной оценке (дефект 5)."""

    def test_total_unknown_without_quote(self):
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "3", "100", commission="0", minutes=-10)
        p.refresh_from_db()
        summary = portfolio_summary(p)
        self.assertTrue(summary["incomplete"])
        self.assertIsNone(summary["total_value"])
        self.assertIsNone(summary["unrealized_pl"])
        self.assertIsNone(summary["overall_pl"])
        self.assertIsNone(summary["return_pct"])
        # известные части остаются
        self.assertEqual(summary["cash"], Decimal("700"))
        self.assertEqual(summary["market_value"], Decimal("0"))

    def test_http_shows_dash_for_total(self):
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "3", "100", commission="0", minutes=-10)
        p.refresh_from_db()
        self.client.force_login(self.user)
        r = self.client.get(reverse("dashboard:portfolio-detail", args=[p.pk]))
        self.assertContains(r, "— (неполная оценка)")
        self.assertContains(r, "700.0000")  # известный cash

    def test_empty_portfolio_fully_valued(self):
        p = self.make_portfolio(initial="1000")
        summary = portfolio_summary(p)
        self.assertFalse(summary["incomplete"])
        self.assertEqual(summary["total_value"], Decimal("1000"))


class PortfolioNameDuplicatesTests(PortfolioBase):
    """Повтор названия — понятная ошибка формы, не 500 (дефект 6)."""

    def test_duplicate_name_same_owner_form_error(self):
        self.make_portfolio(name="Alpha")
        self.client.force_login(self.user)
        r = self.client.post(
            reverse("dashboard:portfolio-create"),
            {
                "name": "Alpha",
                "base_currency": "USD",
                "valuation_source": "demo",
                "initial_cash": "100",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "таким названием")
        self.assertEqual(Portfolio.objects.filter(user=self.user).count(), 1)

    def test_same_name_other_owner_allowed(self):
        self.make_portfolio(name="Alpha")
        self.client.force_login(self.other_user)
        r = self.client.post(
            reverse("dashboard:portfolio-create"),
            {
                "name": "Alpha",
                "base_currency": "USD",
                "valuation_source": "demo",
                "initial_cash": "100",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Portfolio.objects.filter(name="Alpha").count(), 2)


class TradeFormSourceBadgeTests(PortfolioBase):
    """Источник оценки фактический и на ошибочных ветках (дефект 7)."""

    def test_tinkoff_form_has_no_demo_badge(self):
        p = self.make_portfolio(
            initial="1000", source="tinkoff", name="T-Portfolio"
        )
        self.client.force_login(self.user)
        at = timezone.now().strftime("%Y-%m-%d %H:%M")
        r = self.client.post(
            reverse("dashboard:portfolio-trade-new", args=[p.pk]),
            {
                "share": str(self.share.id),
                "side": "BUY",
                "quantity": "999",  # не пройдёт
                "price": "100",
                "commission": "0",
                "executed_at": at,
            },
        )
        self.assertEqual(r.status_code, 200)
        # ошибка показана, DEMO-бейдж при tinkoff-источнике отсутствует
        self.assertNotContains(r, "badge bg-warning text-dark")
        self.assertNotContains(r, "DEMO")
        # и на начальном GET тоже
        r = self.client.get(
            reverse("dashboard:portfolio-trade-new", args=[p.pk])
        )
        self.assertNotContains(r, "DEMO")
        p.refresh_from_db()
        self.assertEqual(p.trades.count(), 0)


# --- Вторая ревизия: округление средней/хвостовые нули/предел точности ------


class AverageCostRoundingTests(PortfolioBase):
    """Обычные последовательности операций при округлении средней (п.1).

    Средняя себестоимость 301/3 требует округления в контексте
    производных; денежные суммы и cash остаются точными. Контекст
    производных не должен наследовать trap Inexact от точного
    контекста исполнения (иначе вторая покупка падала decimal.Inexact).
    """

    def test_second_buy_survives_average_rounding(self):
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "3", "100", commission="1", minutes=-30)
        p.refresh_from_db()
        self.assertEqual(p.cash, "699")
        # вторая покупка не падает decimal.Inexact
        self.trade(p, "BUY", "1", "1", commission="0", minutes=-20)
        p.refresh_from_db()
        # cash точен: 699 - 1
        self.assertEqual(p.cash, "698")
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("4"))
        # средняя (302/4) включая комиссию первой покупки
        self.assertEqual(display_round(pos["avg_cost"]), Decimal("75.5000"))
        self.assertEqual(
            display_round(pos["open_qty"] * pos["avg_cost"]),
            Decimal("302.0000"),
        )

    def test_partial_full_sell_and_new_buy(self):
        p = self.make_portfolio(initial="1000")
        self.trade(p, "BUY", "3", "100", commission="1", minutes=-40)
        self.trade(p, "BUY", "1", "1", commission="0", minutes=-30)
        # частичная продажа
        self.trade(p, "SELL", "2", "130", commission="2", minutes=-20)
        p.refresh_from_db()
        # 698 + 2*130 - 2
        self.assertEqual(p.cash, "956")
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("2"))
        # реализованный P/L: 258 - 2*avg(≈75.5)
        self.assertEqual(
            display_round(pos["realized_pl"]), Decimal("107.0000")
        )
        # полная продажа
        self.trade(p, "SELL", "2", "80", commission="1", minutes=-10)
        p.refresh_from_db()
        self.assertEqual(p.cash, "1115")
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("0"))
        self.assertIsNone(pos["avg_cost"])
        # новая покупка после закрытия
        self.trade(p, "BUY", "5", "50", commission="2", minutes=-5)
        p.refresh_from_db()
        self.assertEqual(p.cash, "863")
        pos = compute_positions(p)[self.share.figi]
        self.assertEqual(pos["open_qty"], Decimal("5"))
        # (250 + 2) / 5 — новая средняя с новой комиссией
        self.assertEqual(display_round(pos["avg_cost"]), Decimal("50.4000"))


class TrailingZerosCanonicalizationTests(PortfolioBase):
    """Допустимые хвостовые нули не дают ложного отказа (п.2)."""

    def test_trailing_zero_input_accepts_exact(self):
        p = self.make_portfolio(initial="1000")
        self.trade(
            p,
            "BUY",
            "1.000000000000000000",
            "1.000000000000000000",
            commission="0",
            minutes=-10,
        )
        p.refresh_from_db()
        self.assertEqual(p.cash, "999")
        tr = p.trades.get()
        # журнал — канонический текст (хвостовые нули сняты)
        self.assertEqual(tr.quantity, "1")
        self.assertEqual(tr.price, "1")

    def test_retry_after_db_read_idempotent(self):
        p = self.make_portfolio(initial="1000")
        t1 = self.trade(
            p,
            "BUY",
            "1.000000000000000000",
            "1.000000000000000000",
            commission="0",
            minutes=-10,
            idempotency_key="tz1",
        )
        p.refresh_from_db()
        # повтор со свежим объектом из БД, тем же executed_at прежней
        # операции (payload включает время) и текстом без хвостовых нулей
        fresh = Portfolio.objects.get(id=p.id)
        t2 = self.trade(
            fresh,
            "BUY",
            "1",
            "1",
            commission="0",
            idempotency_key="tz1",
            at=t1.executed_at,
        )
        self.assertEqual(t2.pk, t1.pk)
        p.refresh_from_db()
        self.assertEqual(p.cash, "999")
        self.assertEqual(p.trades.count(), 1)

    def test_canonicalization_independent_of_decimal_context(self):
        # специально: normalize() в стандартном контексте 28 округлил бы
        # большие точные значения; канонизация сервиса — строковая,
        # без округления.
        from decimal import getcontext, localcontext

        big = Decimal("123456789012345678901234567890.1")
        self.assertEqual(
            canonical_text_for_stored(big),
            "123456789012345678901234567890.1",
        )
        with localcontext(getcontext()):
            getcontext().prec = 28
            self.assertNotEqual(
                str(big.normalize()),
                "123456789012345678901234567890.1",
            )

    def test_big_exact_values_trade_and_idempotent(self):
        p = self.make_portfolio(initial="234567890123456789012345678901")
        t1 = self.trade(
            p,
            "BUY",
            "123456789012345678901234567890.1",
            "1",
            commission="0",
            minutes=-10,
            idempotency_key="big1",
        )
        p.refresh_from_db()
        # эталон: независимая арифметика с достаточной точностью
        from decimal import localcontext as _lc

        with _lc() as ref:
            ref.prec = 80
            expected_cash = Decimal(
                "234567890123456789012345678901"
            ) - Decimal("123456789012345678901234567890.1")
        self.assertEqual(p.cash_decimal, expected_cash)
        self.assertEqual(p.cash, "111111101111111110111111111010.9")
        tr = p.trades.get()
        self.assertEqual(tr.quantity, "123456789012345678901234567890.1")
        # повтор с прежними данными и тем же executed_at (payload
        # включает время), после чтения из БД — прежняя запись, без
        # дубля и без изменения cash
        fresh = Portfolio.objects.get(id=p.id)
        t2 = self.trade(
            fresh,
            "BUY",
            "123456789012345678901234567890.1",
            "1",
            commission="0",
            idempotency_key="big1",
            at=t1.executed_at,
        )
        self.assertEqual(t2.pk, t1.pk)
        p.refresh_from_db()
        self.assertEqual(p.trades.count(), 1)
        self.assertEqual(p.cash_decimal, expected_cash)


class PrecisionLimitTests(PortfolioBase):
    """Заявленный предел 18 дробных знаков проверяется везде (п.3)."""

    def test_service_rejects_1e_minus20_quantity(self):
        p = self.make_portfolio(initial="1000")
        with self.assertRaises(ValueError) as ctx:
            self.trade(
                p,
                "BUY",
                "0.00000000000000000001",
                "100000000000000000000",
                commission="0",
                minutes=-10,
            )
        self.assertIn("точности", str(ctx.exception))
        p.refresh_from_db()
        # без частичных изменений
        self.assertEqual(p.cash, "1000")
        self.assertEqual(p.trades.count(), 0)

    def test_trade_form_rejects_1e_minus20_quantity(self):
        p = self.make_portfolio(initial="1000")
        self.client.force_login(self.user)
        at = timezone.now().strftime("%Y-%m-%d %H:%M")
        r = self.client.post(
            reverse("dashboard:portfolio-trade-new", args=[p.pk]),
            {
                "share": str(self.share.id),
                "side": "BUY",
                "quantity": "0.00000000000000000001",
                "price": "100000000000000000000",
                "commission": "0",
                "executed_at": at,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "точности")
        p.refresh_from_db()
        self.assertEqual(p.cash, "1000")
        self.assertEqual(p.trades.count(), 0)

    def test_initial_cash_1e_minus24_rejected_by_form(self):
        self.client.force_login(self.user)
        r = self.client.post(
            reverse("dashboard:portfolio-create"),
            {
                "name": "Tiny",
                "base_currency": "USD",
                "valuation_source": "demo",
                "initial_cash": "0.000000000000000000000001",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "точности")
        self.assertEqual(
            Portfolio.objects.filter(user=self.user, name="Tiny").count(),
            0,
        )

    def test_trailing_zeros_do_not_increase_required_precision(self):
        # 18 дробных знаков, все — хвостовые нули: допустимо
        self.trade(
            self.make_portfolio(initial="1000"),
            "BUY",
            "1.000000000000000000",
            "1",
            commission="0.000000000000000000",  # тоже хвостовые нули
            minutes=-10,
        )
