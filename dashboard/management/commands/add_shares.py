from django.core.management.base import BaseCommand

from dashboard.models import Share
from dashboard.services.market_data import (
    apply_rpc_deadline,
    get_tinkoff_timeout,
    open_tinkoff_client,
    run_with_timeout,
)


class Command(BaseCommand):
    help = "Add shares to DB"

    def handle(self, *args, **options):
        try:
            client_ctx = open_tinkoff_client()
        except Exception as e:
            # отсутствие токена и другие проблемы до установления соединения
            self.stderr.write(f"add_shares: {e}")
            return

        try:
            with client_ctx as client:
                # Реальный внешний запрос ограничен настоящим gRPC-дедлайном
                # плюс локальное ожидание.
                apply_rpc_deadline(client, get_tinkoff_timeout())
                shares_response = run_with_timeout(
                    lambda: client.instruments.shares(),
                    get_tinkoff_timeout(),
                    "instruments.shares",
                )
        except Exception as e:
            # сбои самого запроса (сеть, API, таймаут) — явный вывод ошибки,
            # не падение с traceback
            self.stderr.write(f"add_shares: {e}")
            return

        created, updated = 0, 0
        for share in shares_response.instruments:
            # Ключ поиска — figi (идентичность источника; сама она не меняется
            # при обновлении каталога). fix 4: у акции, у которой накоплена
            # история наблюдений, запрещено менять идентичность (uid/ticker)
            # и валюту — это подменяет инструмент, к которому относится
            # история; такие поля пропускаются с журналированием.
            existing = Share.objects.filter(figi=share.figi).first()
            if existing is None:
                Share.objects.create(
                    figi=share.figi,
                    name=share.name,
                    isin=share.isin,
                    ticker=share.ticker,
                    currency=share.currency,
                    uid=share.uid,
                )
                created += 1
                continue
            changed_fields = {
                "name": share.name,
                "isin": share.isin,
            }
            protected = existing.protected_identity_changes(
                {
                    "ticker": share.ticker,
                    "currency": share.currency,
                    "uid": share.uid,
                }
            )
            if protected:
                self.stdout.write(
                    f"add_shares: пропуск защищённых полей {protected} для "
                    f"{share.ticker} (у акции накоплена история цен)"
                )
            else:
                for field, value in (
                    ("ticker", share.ticker),
                    ("currency", share.currency),
                    ("uid", share.uid),
                ):
                    changed_fields[field] = value
            if (
                Share.objects.filter(figi=share.figi)
                .exclude(**changed_fields)
                .update(**changed_fields)
            ):
                updated += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"add_shares created: {created}, updated: {updated}"
            )
        )
