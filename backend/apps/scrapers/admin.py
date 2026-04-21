from django.contrib import admin
from . import models
from django.db.models import Q
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from regex import P

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
    readonly_fields = ("created_at",)
    search_fields = ("task_name",)


class HaveContentListFilter(admin.SimpleListFilter):
    title = _("have content ru")

    parameter_name = "content_ru"

    def lookups(self, request, model_admin):
        return [("есть", _("есть")), ("нет", _("нет"))]

    def queryset(self, request, queryset):
        if self.value() == "есть":
            return queryset.filter(film_content__isnull=False)
        if self.value() == "нет":
            return queryset.filter(film_content__isnull=True)


class HaveEnglishContentListFilter(admin.SimpleListFilter):
    title = _("have content eng")

    parameter_name = "content_eng"

    def lookups(self, request, model_admin):
        return [("есть", _("есть")), ("нет", _("нет"))]

    def queryset(self, request, queryset):
        if self.value() == "есть":
            return queryset.filter(audio_tracks__icontains="eng.original")
        if self.value() == "нет":
            return queryset.exclude(audio_tracks__icontains="eng.original")


class ExactSearchFilter(admin.SimpleListFilter):
    title = "Точный поиск"
    parameter_name = "exact_search"

    def lookups(self, request, model_admin):
        return [("enabled", "Включить")]

    def queryset(self, request, queryset):
        search_term = request.GET.get("q")
        if self.value() == "enabled" and search_term:
            return queryset.filter(Q(id=search_term) | Q(kino_poisk_id=search_term))
        return queryset


@admin.register(models.Content)
class ContentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name_ru",
        "kino_poisk_id",
        "is_serial",
    )
    autocomplete_fields = (
        "countries",
        "genres",
        "keywords",
        "studios",
        "collections",
    )
    search_fields = ("name_ru", "kino_poisk_id", "id")
    readonly_fields = ("get_related_actors",)
    list_filter = (
        "is_serial",
        HaveContentListFilter,
        HaveEnglishContentListFilter,
        "is_parsed_kp",
        ExactSearchFilter,
    )

    class Media:
        css = {"all": ("css/admin.css",)}

    def get_related_actors(self, obj):
        actors = obj.content_actors.all()
        if not actors:
            return "<p>Нет связанных актеров</p>"

        actor_list = []
        for actor in actors:
            actor_list.append(
                f'<div class="actor-item">'
                f'<span class="actor-title">name - </span><span class="actor-name">{actor.participant}</span>'
                f'<span class="actor-title">role - </span><span class="actor-role">{actor.role}</span>'
                f'<span class="actor-title">ordering - </span><span class="actor-ordering">{actor.ordering}</span>'
                "</div>"
            )

        return format_html('<div class="actor-list">' + "".join(actor_list) + "</div>")

    get_related_actors.short_description = "Актеры"

    def get_actors_count(self, obj):
        return obj.actors.all().count()


@admin.register(models.Country)
class CountryAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.Genre)
class GenreAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "participant_id",
        "name",
        "is_completed",
    )
    filter_horizontal = ("genres",)
    search_fields = ("participant_id", "name", "id")


@admin.register(models.Studio)
class StudioAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.Collection)
class CollectionAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.Platform)
class PlatformAdmin(admin.ModelAdmin):
    pass


@admin.register(models.Keyword)
class KeywordAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(models.Award)
class AwardAdmin(admin.ModelAdmin):
    pass


@admin.register(models.AwardYear)
class AwardYearAdmin(admin.ModelAdmin):
    pass


@admin.register(models.AwardYearNomination)
class AwardYearNominationAdmin(admin.ModelAdmin):
    list_display = ("award_year", "name", "winner_content", "winner_participant")
    list_filter = ("award_year__award",)
    autocomplete_fields = (
        "winner_content",
        "winner_participant",
        "nomination_content",
        "nomination_participant",
    )
