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
            # Ключ поиска — figi; остальные поля (в т.ч. уникальные
            # ticker/uid) при изменении на стороне API обновляются
            # вместо падения с IntegrityError.
            _, was_created = Share.objects.get_or_create(
                figi=share.figi,
                defaults={
                    "name": share.name,
                    "isin": share.isin,
                    "ticker": share.ticker,
                    "currency": share.currency,
                    "uid": share.uid,
                },
            )
            if not was_created:
                changed = (
                    Share.objects.filter(figi=share.figi)
                    .exclude(
                        name=share.name,
                        isin=share.isin,
                        ticker=share.ticker,
                        currency=share.currency,
                        uid=share.uid,
                    )
                    .update(
                        name=share.name,
                        isin=share.isin,
                        ticker=share.ticker,
                        currency=share.currency,
                        uid=share.uid,
                    )
                )
                if changed:
                    updated += 1
            else:
                created += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"add_shares created: {created}, updated: {updated}"
            )
        )
