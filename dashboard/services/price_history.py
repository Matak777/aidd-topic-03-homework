from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from plotly.graph_objs import Scatter
from plotly.offline import plot

from dashboard.models import PriceObservation

PRICE_PERIODS = (
    ("1d", "24 часа"),
    ("7d", "7 дней"),
    ("30d", "30 дней"),
    ("90d", "90 дней"),
    ("365d", "1 год"),
    ("all", "Всё время"),
)

_PERIOD_LENGTHS = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "365d": timedelta(days=365),
}


@dataclass(frozen=True)
class PriceSeries:
    source: str
    source_label: str
    currency: str
    observations: list[PriceObservation]
    first: PriceObservation | None
    latest: PriceObservation | None
    absolute_change: Decimal | None
    percentage_change: Decimal | None


@dataclass(frozen=True)
class PriceHistory:
    period: str
    observations: list[PriceObservation]
    series: list[PriceSeries]


def get_price_history(share, period, *, now=None):
    if period not in dict(PRICE_PERIODS):
        raise ValueError(f"Unsupported price history period: {period}")

    end = now or timezone.now()
    observations = PriceObservation.objects.filter(
        share=share,
        observed_at__lte=end,
    )
    if period != "all":
        observations = observations.filter(
            observed_at__gte=end - _PERIOD_LENGTHS[period]
        )
    observations = list(observations.order_by("observed_at", "pk"))

    grouped = {}
    for observation in observations:
        grouped.setdefault((observation.source, observation.currency), []).append(
            observation
        )

    series = []
    for (source, currency), points in grouped.items():
        first = points[0]
        latest = points[-1]
        absolute_change = None
        percentage_change = None
        if len(points) >= 2:
            absolute_change = latest.price - first.price
            if first.price != 0:
                percentage_change = (
                    absolute_change / first.price * Decimal("100")
                ).quantize(Decimal("0.01"))
        series.append(
            PriceSeries(
                source=source,
                source_label=dict(PriceObservation.Source.choices)[source],
                currency=currency,
                observations=points,
                first=first,
                latest=latest,
                absolute_change=absolute_change,
                percentage_change=percentage_change,
            )
        )

    return PriceHistory(
        period=period,
        observations=observations,
        series=series,
    )


def build_price_history_graph(series):
    traces = [
        Scatter(
            x=[observation.observed_at for observation in item.observations],
            y=[float(observation.price) for observation in item.observations],
            mode="lines+markers",
            name=(
                f"{item.source_label} "
                f"({item.currency})"
            ),
        )
        for item in series
    ]
    if not traces:
        return ""

    return plot(
        traces,
        output_type="div",
        include_plotlyjs="cdn",
        show_link=False,
        link_text="",
    )
