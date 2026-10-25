import math
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.tasks import _update_last_price


class Command(BaseCommand):
    help = "Periodically collect and persist prices without requiring Redis."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=float,
            help="Seconds between cycles (defaults to PRICE_COLLECTION_INTERVAL_SECONDS).",
        )
        parser.add_argument(
            "--max-cycles",
            type=int,
            help="Stop after this many cycles; omit to run until interrupted.",
        )
        parser.add_argument(
            "--share-id",
            type=int,
            help="Collect only one share (useful for a bounded demonstration).",
        )

    def handle(self, *args, **options):
        interval = (
            settings.PRICE_COLLECTION_INTERVAL_SECONDS
            if options["interval"] is None
            else options["interval"]
        )
        max_cycles = options["max_cycles"]
        if not math.isfinite(interval) or interval <= 0:
            raise CommandError("--interval must be a positive finite number.")
        if max_cycles is not None and max_cycles < 1:
            raise CommandError("--max-cycles must be a positive integer.")
        if settings.PRICE_DATA_MODE == "live" and not settings.TINKOFF_TOKEN:
            raise CommandError(
                "PRICE_DATA_MODE=live requires TINKOFF_TOKEN. "
                "Set a token or explicitly enable PRICE_DATA_MODE=demo."
            )

        stop_event = threading.Event()
        previous_handlers = {}

        def request_stop(signum, frame):
            stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

        cycle = 0
        try:
            if settings.PRICE_DATA_MODE == "demo":
                self.stdout.write(
                    self.style.WARNING(
                        "Демонстрационный режим: будут записаны синтетические цены."
                    )
                )
            else:
                self.stdout.write("Источник цен: Tinkoff Invest.")
            while not stop_event.is_set():
                updated = _update_last_price(
                    share_id=options["share_id"],
                    stop_event=stop_event,
                )
                cycle += 1
                self.stdout.write(
                    f"Цикл {cycle}: успешно получено цен — {updated}."
                )
                if max_cycles is not None and cycle >= max_cycles:
                    break
                stop_event.wait(interval)
        except KeyboardInterrupt:
            self.stdout.write("Сбор цен остановлен пользователем.")
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

        self.stdout.write(self.style.SUCCESS("Сбор цен завершён."))
