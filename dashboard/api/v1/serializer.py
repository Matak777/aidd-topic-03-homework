from rest_framework import serializers

from dashboard.models import Share


class ShareSerializer(serializers.ModelSerializer):
    # Происхождение текущей цены (fix 3): клиент видит, откуда пришло
    # значение last_price (demo — синтетика, manual — ввод вручную,
    # tinkoff — рынок; null — неизвестно, например записи до миграции).
    last_price_source = serializers.CharField(read_only=True)

    class Meta:
        model = Share
        fields = (
            "id",
            "figi",
            "isin",
            "name",
            "ticker",
            "currency",
            "uid",
            "last_price",
            "last_price_source",
            "is_trend_high",
            "updated_at",
        )

    def create(self, validated_data):
        # Происхождение цены — место ввода, не догадки: явная цена
        # (в т.ч. нулевая) — ручной ввод; явный null — цены нет и источника
        # нет (нет цены — нет источника).
        if "last_price" in validated_data:
            validated_data["last_price_source"] = (
                Share.LAST_PRICE_SOURCE_MANUAL
                if validated_data["last_price"] is not None
                else None
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Явная цена в PATCH/PUT (в т.ч. 0) — ручное изменение; null —
        # очистка, источник снимается. Без цены в запросе существующее
        # происхождение сохраняется.
        if "last_price" in validated_data:
            validated_data["last_price_source"] = (
                Share.LAST_PRICE_SOURCE_MANUAL
                if validated_data["last_price"] is not None
                else None
            )
        protected = instance.protected_identity_changes(validated_data)
        if protected:
            raise serializers.ValidationError(
                {
                    field: (
                        "У акции уже накоплена история наблюдений цен: "
                        "смена поля подменяет инструмент, к которому "
                        "относится история. Создайте новый инструмент "
                        "с отдельной записью либо удалите историю явно."
                    )
                    for field in protected
                }
            )
        return super().update(instance, validated_data)
