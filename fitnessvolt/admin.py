from django.contrib import admin

from .models import FitnessVoltLiftAlias, FitnessVoltStandardCache


@admin.register(FitnessVoltStandardCache)
class FitnessVoltStandardCacheAdmin(admin.ModelAdmin):
    list_display = [
        "population",
        "lift_slug",
        "sex",
        "weight_class_label",
        "source_snapshot_version",
        "sample_size",
        "fetched_at",
    ]
    list_filter = ["population", "sex", "source_snapshot_version"]
    ordering = ["population", "lift_slug", "sex", "weight_class_kg"]
    search_fields = ["lift_slug", "weight_class_label"]


@admin.register(FitnessVoltLiftAlias)
class FitnessVoltLiftAliasAdmin(admin.ModelAdmin):
    list_display = ["from_slug", "to_name"]
    ordering = ["from_slug"]
    search_fields = ["from_slug", "to_name"]
