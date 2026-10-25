from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

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

        try:
            with transaction.atomic():
                for remote_share in shares:
                    figi_match = Share.objects.filter(
                        figi=remote_share.figi
                    ).first()
                    uid_match = Share.objects.filter(uid=remote_share.uid).first()

                    if (
                        figi_match is not None
                        and uid_match is not None
                        and figi_match.pk != uid_match.pk
                    ):
                        raise CommandError(
                            "Конфликт каталога: FIGI и UID указывают на разные "
                            f"инструменты ({remote_share.figi}, {remote_share.uid})."
                        )

                    share = figi_match or uid_match

                    if share is None:
                        Share.objects.create(
                            figi=remote_share.figi,
                            name=remote_share.name,
                            isin=remote_share.isin,
                            ticker=remote_share.ticker,
                            currency=remote_share.currency,
                            uid=remote_share.uid,
                        )
                        continue

                    previous = Share.objects.get(pk=share.pk)
                    share.figi = remote_share.figi
                    share.name = remote_share.name
                    share.isin = remote_share.isin
                    share.ticker = remote_share.ticker
                    share.currency = remote_share.currency
                    share.uid = remote_share.uid
                    share.apply_update_policy(previous)
                    share.save()
        except ValidationError as error:
            raise CommandError(
                "Каталог содержит изменение идентификатора инструмента с "
                f"сохранённой историей: {error}"
            ) from error
        except IntegrityError as error:
            raise CommandError(
                "Каталог содержит конфликт уникальных идентификаторов "
                "(FIGI, UID или тикер); импорт отменён без частичных изменений."
            ) from error
