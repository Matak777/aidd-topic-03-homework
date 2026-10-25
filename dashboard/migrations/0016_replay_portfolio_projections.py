from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from itertools import groupby

from django.db import migrations


QUANTUM = Decimal("0.000000001")


def quantize(value):
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def replay_portfolio_projections(apps, schema_editor):
    Position = apps.get_model("dashboard", "PortfolioPosition")
    Transaction = apps.get_model("dashboard", "PortfolioTransaction")
    database = schema_editor.connection.alias

    with localcontext() as context:
        context.prec = 80

        positions = {
            (position.portfolio_id, position.share_id): position
            for position in Position.objects.using(database)
            .exclude(share_id=None)
            .iterator()
        }
        transactions = (
            Transaction.objects.using(database)
            .exclude(share_id=None)
            .order_by("portfolio_id", "share_id", "executed_at", "pk")
        )
        grouped_transactions = groupby(
            transactions.iterator(chunk_size=1000),
            key=lambda record: (record.portfolio_id, record.share_id),
        )
        for key, records in grouped_transactions:
            portfolio_id, share_id = key
            position = positions.get(key)

            quantity = Decimal("0")
            cost_basis = Decimal("0")
            realized_pnl = Decimal("0")
            latest = None

            for record in records:
                latest = record
                gross = quantize(record.quantity * record.execution_price)
                if record.side == "buy":
                    quantity = quantize(quantity + record.quantity)
                    cost_basis = quantize(
                        cost_basis + quantize(gross + record.commission)
                    )
                    continue

                if record.side != "sell":
                    raise RuntimeError(
                        "Невозможно восстановить портфельную проекцию: "
                        f"неизвестная сторона операции {record.pk}."
                    )
                if record.quantity > quantity:
                    raise RuntimeError(
                        "Невозможно восстановить портфельную проекцию: "
                        f"операция {record.pk} продаёт больше доступного остатка."
                    )

                proceeds = quantize(gross - record.commission)
                if record.quantity == quantity:
                    disposed_basis = cost_basis
                    quantity = Decimal("0")
                    cost_basis = Decimal("0")
                else:
                    disposed_basis = quantize(
                        cost_basis * record.quantity / quantity
                    )
                    quantity = quantize(quantity - record.quantity)
                    cost_basis = quantize(cost_basis - disposed_basis)
                realized_pnl = quantize(
                    realized_pnl + quantize(proceeds - disposed_basis)
                )
            if position is None:
                position = Position(
                    portfolio_id=portfolio_id,
                    share_id=share_id,
                    share_ticker=latest.share_ticker,
                    share_name=latest.share_name,
                )
            position.quantity = quantity
            position.cost_basis = cost_basis
            position.average_cost = (
                quantize(cost_basis / quantity)
                if quantity
                else Decimal("0")
            )
            position.realized_pnl = realized_pnl
            position.save(using=database)

    # Rows whose Share FK was deleted cannot be assigned to a unique instrument
    # from the remaining snapshots; preserve their existing projections.


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0015_portfolioposition_portfolio_position_cost_basis_nonnegative"),
    ]

    operations = [
        migrations.RunPython(
            replay_portfolio_projections,
            migrations.RunPython.noop,
        ),
    ]
