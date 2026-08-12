from django.contrib import admin

from .models import HevyLiftAlias


@admin.register(HevyLiftAlias)
class HevyLiftAliasAdmin(admin.ModelAdmin):
    list_display = ["from_name", "to_name"]
    ordering = ["from_name"]
    search_fields = ["from_name", "to_name"]
