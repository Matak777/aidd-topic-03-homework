import threading
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

import dashboard.tasks as tasks
from dashboard.models import Share


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
        with patch("dashboard.tasks.get_last_prices", return_value=prices):
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
            "dashboard.tasks.get_last_prices", side_effect=RuntimeError("net")
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
