from django.contrib import admin
from . import models

# Register your models here.


@admin.register(models.YtConnectContent)
class YtConnectContentAdmin(admin.ModelAdmin):
    list_display = ("content_id", "parsing_status", "updated_at")
    list_filter = ("parsing_status",)
    search_fields = ("content_id",)


@admin.register(models.ScraperLog)
class ScraperLogAdmin(admin.ModelAdmin):
    list_display = ("task_name", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("task_name",)


@admin.register(models.ContentAppContent)
class ContentAdmin(admin.ModelAdmin):
    list_display = ("id", "name_ru", "kino_poisk_id", "is_serial")
    search_fields = ("kino_poisk_id", "name_ru")
