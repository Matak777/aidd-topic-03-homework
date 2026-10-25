from django.core.exceptions import ValidationError
from rest_framework import serializers

from dashboard.models import Share


class ShareSerializer(serializers.ModelSerializer):
    last_price_source = serializers.SerializerMethodField()

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
            "last_price_observed_at",
            "is_trend_high",
            "updated_at",
        )
        read_only_fields = ("last_price_observed_at",)

    def get_last_price_source(self, instance):
        if instance.last_price is None:
            return None
        return instance.last_price_source or "unknown"

    def validate(self, attrs):
        if self.instance is not None:
            candidate = Share.objects.get(pk=self.instance.pk)
            for field, value in attrs.items():
                setattr(candidate, field, value)
            try:
                candidate.apply_update_policy(
                    self.instance,
                    explicit_price="last_price" in attrs,
                )
            except ValidationError as error:
                raise serializers.ValidationError(error.message_dict) from error
            for field in (
                "last_price",
                "last_price_source",
                "last_price_observed_at",
                "is_trend_high",
            ):
                if getattr(candidate, field) != getattr(self.instance, field):
                    attrs[field] = getattr(candidate, field)
        return attrs

    def create(self, validated_data):
        if validated_data.get("last_price") is not None:
            validated_data["last_price_source"] = Share.PriceSource.MANUAL
            validated_data["last_price_observed_at"] = None
            validated_data["is_trend_high"] = None
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "last_price" in validated_data:
            if validated_data["last_price"] is None:
                validated_data["last_price_source"] = None
                validated_data["last_price_observed_at"] = None
            else:
                validated_data["last_price_source"] = Share.PriceSource.MANUAL
                validated_data["last_price_observed_at"] = None
        return super().update(instance, validated_data)
