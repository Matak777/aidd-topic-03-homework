from datetime import timedelta

from plotly.graph_objs import Scatter
from plotly.offline import plot
from tinkoff.invest import CandleInterval
from tinkoff.invest.utils import now

from dashboard.services.market_data import (
    apply_rpc_deadline,
    get_tinkoff_timeout,
    money_to_float,
    open_tinkoff_client,
    run_with_timeout,
)


def get_graph(share):
    """Строит HTML-график (Plotly) цены акции за 180 дней (дневные свечи)."""
    x_data, y_data = [], []
    deadline = get_tinkoff_timeout()

    def fetch_candles():
        with open_tinkoff_client() as client:
            # Каждая RPC-страница get_all_candles ограничена собственным
            # gRPC-дедлайном; суммарное локальное ожидание ограничено
            # run_with_timeout.
            apply_rpc_deadline(client, deadline)
            collected = []
            for candle in client.get_all_candles(
                figi=share.figi,
                from_=now() - timedelta(days=180),
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            ):
                collected.append((candle.time.date(), candle.close))
            return collected

    candles = run_with_timeout(fetch_candles, deadline, "get_graph")
    for candle_date, close in candles:
        x_data.append(candle_date)
        # close — Quotation (units + nano): учитываем дробную часть целиком.
        y_data.append(money_to_float(close))

    plot_div = plot(
        [
            Scatter(
                x=x_data,
                y=y_data,
                mode="lines",
                name="test",
                opacity=0.8,
                marker_color="red",
            )
        ],
        output_type="div",
        show_link=False,
        link_text="",
    )
    return plot_div
