"""Периодический локальный сбор рыночных цен в историю цен БД.

Не требует Redis: сам цикл (получение цен → запись наблюдений) выполняется
в этом процессе с настраиваемым интервалом. Redis-вариант (Celery beat +
задача update_last_price) сохранён как альтернатива.

Режимы источника:
- tinkoff (значение по умолчанию, настройка PRICE_SOURCE): реальный API
  Tinkoff; без токена цикл честно журналирует недоступность и продолжается;
- demo: ЯВНО включаемый синтетический режим без токена; наблюдения
  помечаются источником "demo" и показываются в интерфейсе как
  демонстрационные данные.

Штатная остановка: --max-cycles N, Ctrl-C (KeyboardInterrupt) или SIGTERM.
"""

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.services import price_history
from dashboard.services.price_history import (
    SOURCE_DEMO,
    SOURCE_TINKOFF,
    validate_interval,
)


def _terminate(_signum, _frame):
    raise KeyboardInterrupt  # штатная остановка по SIGTERM


class Command(BaseCommand):
    help = (
        "Периодический сбор рыночных цен в накопленную историю "
        "(PriceObservation) без обязательного Redis."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=float,
            default=None,
            help="Секунды между циклами (по умолчанию PRICE_COLLECT_INTERVAL)",
        )
        parser.add_argument(
            "--source",
            choices=("auto", SOURCE_TINKOFF, SOURCE_DEMO),
            default="auto",
            help=(
                "Источник цен: auto — из настройки PRICE_SOURCE; demo — "
                "ЯВНО включаемый синтетический режим; tinkoff — реальный API"
            ),
        )
        parser.add_argument(
            "--max-cycles",
            type=int,
            default=0,
            help="Остановиться после N циклов (0 — бесконечно; для проверки)",
        )

    def handle(self, *args, **options):
        interval = (
            options["interval"]
            if options["interval"] is not None
            else settings.PRICE_COLLECT_INTERVAL
        )
        try:
            interval = validate_interval(interval, "Интервал коллекционера")
        except ValueError as e:
            raise CommandError(str(e))

        source_override = options["source"]
        source = price_history.resolve_source(
            None if source_override == "auto" else source_override
        )
        max_cycles = options["max_cycles"] or 0
        if max_cycles < 0:
            raise CommandError("--max-cycles должен быть неотрицательным")

        try:
            old_sigterm = signal.signal(signal.SIGTERM, _terminate)
        except ValueError:
            # не в главном потоке (например, тестовый контекст) — без сигналов
            old_sigterm = None
        self.stdout.write(
            f"collect_prices: старт (источник={source}, интервал={interval} с)"
        )
        cycles = 0
        try:
            while True:
                try:
                    created, total = price_history.collect_cycle(source)
                    self.stdout.write(
                        f"  цикл {cycles + 1}: наблюдений записано "
                        f"{created}, акций в каталоге {total}"
                    )
                except Exception as e:
                    # сбой цикла (сеть, нет токена, API) — журнализация,
                    # коллектор не умирает: следующий цикл повторит попытку
                    self.stderr.write(f"collect_prices: сбой цикла: {e}")
                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    self.stdout.write("collect_prices: лимит циклов достигнут")
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("collect_prices: штатная остановка")
        finally:
            if old_sigterm is not None:
                signal.signal(signal.SIGTERM, old_sigterm)
