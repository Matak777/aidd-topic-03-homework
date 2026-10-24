from datetime import timedelta

from plotly.graph_objs import Scatter
from plotly.offline import plot

from .market_data import MarketDataError, _provider_error, get_client


def get_graph(share):
    try:
        client_context = get_client()
        from tinkoff.invest import CandleInterval
        from tinkoff.invest.utils import now

        with client_context as client:
            candles = list(
                client.get_all_candles(
                    figi=share.figi,
                    from_=now() - timedelta(days=180),
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                )
            )
    except MarketDataError:
        raise
    except Exception as error:
        raise _provider_error(
            error,
            f"получении истории котировок для {share.ticker}",
            f"Не удалось получить историю котировок для {share.ticker}.",
        ) from error

    if not candles:
        raise MarketDataError(
            f"Tinkoff API не вернул историю котировок для {share.ticker}."
        )

    plot_div = plot(
        [
            Scatter(
                x=[candle.time.date() for candle in candles],
                y=[
                    candle.close.units + candle.close.nano / 1_000_000_000
                    for candle in candles
                ],
                mode="lines",
                name=share.ticker,
                opacity=0.8,
                marker_color="red",
            )
        ],
        output_type="div",
        include_plotlyjs="cdn",
        show_link=False,
        link_text="",
    )
    return plot_div
