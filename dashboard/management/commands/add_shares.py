from django.core.management.base import BaseCommand, CommandError

from dashboard.models import Share
from dashboard.services.market_data import MarketDataError, _provider_error, get_client

class Command(BaseCommand):
    help = "Add shares to DB"

    def handle(self, *args, **options):
        try:
            with get_client() as client:
                shares = list(client.instruments.shares().instruments)
        except MarketDataError as error:
            raise CommandError(str(error)) from error
        except Exception as error:
            market_error = _provider_error(
                error,
                "загрузке каталога акций",
                "Не удалось загрузить акции из Tinkoff API.",
            )
            raise CommandError(str(market_error)) from error

        for share in shares:
            Share.objects.update_or_create(
                figi=share.figi,
                defaults={
                    "name": share.name,
                    "isin": share.isin,
                    "ticker": share.ticker,
                    "currency": share.currency,
                    "uid": share.uid,
                },
            )
