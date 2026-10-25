from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from django.db import migrations, models


QUANTUM = Decimal("0.000000001")


def populate_cost_basis(apps, schema_editor):
    Position = apps.get_model("dashboard", "PortfolioPosition")
    Transaction = apps.get_model("dashboard", "PortfolioTransaction")
    database = schema_editor.connection.alias

    for position in Position.objects.using(database).all().iterator():
        with localcontext() as context:
            context.prec = 80
            if position.share_id is None:
                basis = (position.average_cost * position.quantity).quantize(
                    QUANTUM,
                    rounding=ROUND_HALF_EVEN,
                )
            else:
                quantity = Decimal("0")
                basis = Decimal("0")
                transactions = Transaction.objects.using(database).filter(
                    portfolio_id=position.portfolio_id,
                    share_id=position.share_id,
                ).order_by("executed_at", "pk")
                for record in transactions.iterator():
                    if record.side == "buy":
                        quantity += record.quantity
                        basis += (
                            record.quantity * record.execution_price
                            + record.commission
                        )
                    elif record.quantity == quantity:
                        quantity = Decimal("0")
                        basis = Decimal("0")
                    else:
                        disposed_basis = (
                            basis * record.quantity / quantity
                        ).quantize(
                            QUANTUM,
                            rounding=ROUND_HALF_EVEN,
                        )
                        quantity -= record.quantity
                        basis -= disposed_basis
                basis = basis.quantize(
                    QUANTUM,
                    rounding=ROUND_HALF_EVEN,
                )

        position.cost_basis = basis
        position.save(update_fields=("cost_basis",), using=database)


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0013_virtualportfolio_portfoliotransaction_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='portfolioposition',
            name='cost_basis',
            field=models.DecimalField(decimal_places=9, default=0, max_digits=30),
        ),
        migrations.RunPython(populate_cost_basis, migrations.RunPython.noop),
    ]
