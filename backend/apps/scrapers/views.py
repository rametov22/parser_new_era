from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Max, Min, Q
from django.shortcuts import render
from django.utils import timezone

from .models import Content, ScraperLog


def _stats_for_source(source):
    """
    Считает статистику по одному источнику парсинга.
    source: "kp" / "ru" / "uz" — суффиксы полей is_parsed_*, parsed_at_*, parse_count_*.

    Для ru (vavada) — статистика считается ТОЛЬКО по фильмам в окне премьеры
    (settings.PREMIERE дней), так как vavada не парсит все фильмы базы.
    """
    now = timezone.now()

    status_field = f"is_parsed_{source}"
    parsed_at_field = f"parsed_at_{source}"
    count_field = f"parse_count_{source}"

    qs = Content.objects.all()

    # Vavada работает только с премьерами в окне последних PREMIERE дней.
    scope_note = None
    if source == "ru":
        today = now.date()
        premiere_days = getattr(settings, "PREMIERE", 365)
        start_date = today - timedelta(days=premiere_days)
        qs = qs.filter(
            Q(premiere__range=(start_date, today))
            | Q(premiere_ru__range=(start_date, today))
        )
        scope_note = f"только фильмы с премьерой за последние {premiere_days} дн."

    total = qs.count()
    not_parsed = qs.filter(**{status_field: "not_parsed"}).count()
    in_progress = qs.filter(**{status_field: "in_progress"}).count()
    parsed_now = qs.filter(**{status_field: "parsed"}).count()

    never_parsed = qs.filter(**{count_field: 0}).count()
    ever_parsed = total - never_parsed

    oldest_parse = qs.filter(**{f"{parsed_at_field}__isnull": False}).aggregate(
        Min(parsed_at_field)
    )[f"{parsed_at_field}__min"]
    newest_parse = qs.filter(**{f"{parsed_at_field}__isnull": False}).aggregate(
        Max(parsed_at_field)
    )[f"{parsed_at_field}__max"]

    # Распределение по числу циклов: сколько фильмов было спаршено N раз.
    cycles_breakdown = list(
        qs.values(count_field).annotate(n=Count("id")).order_by(count_field)[:15]
    )

    # Минимум по count = сколько ПОЛНЫХ циклов прошёл парсер.
    # Если хоть у одной записи count_field=0, полных циклов = 0.
    min_count = qs.aggregate(Min(count_field))[f"{count_field}__min"] or 0
    max_count = qs.aggregate(Max(count_field))[f"{count_field}__max"] or 0

    # Сколько спаршено за разные периоды
    activity = []
    for label, delta in [
        ("за час", timedelta(hours=1)),
        ("за сутки", timedelta(days=1)),
        ("за 7 дней", timedelta(days=7)),
        ("за 30 дней", timedelta(days=30)),
    ]:
        n = qs.filter(**{f"{parsed_at_field}__gte": now - delta}).count()
        activity.append({"label": label, "count": n})

    # Скорость и ETA до следующего цикла
    parsed_per_hour = activity[0]["count"]
    parsed_per_day = activity[1]["count"]
    parsed_per_week = activity[2]["count"]

    # Записей, ещё не достигших следующего уровня цикла
    next_cycle = min_count + 1
    remaining_to_next_cycle = qs.filter(**{f"{count_field}__lt": next_cycle}).count()
    eta_days = (
        round(remaining_to_next_cycle / parsed_per_day, 1)
        if parsed_per_day > 0
        else None
    )

    # Распределение по циклам с процентами для прогресс-баров
    cycles_breakdown_pct = []
    for row in cycles_breakdown:
        pct = (row["n"] / total * 100) if total else 0
        cycles_breakdown_pct.append(
            {
                "cycle": row[count_field],
                "count": row["n"],
                "pct": pct,
            }
        )

    return {
        "source": source,
        "scope_note": scope_note,
        "total": total,
        "not_parsed": not_parsed,
        "in_progress": in_progress,
        "parsed_now": parsed_now,
        "never_parsed": never_parsed,
        "ever_parsed": ever_parsed,
        "ever_parsed_pct": (ever_parsed / total * 100) if total else 0,
        "never_parsed_pct": (never_parsed / total * 100) if total else 0,
        "oldest_parse": oldest_parse,
        "newest_parse": newest_parse,
        "cycles_breakdown": cycles_breakdown_pct,
        "full_cycles_completed": min_count,
        "max_cycle": max_count,
        "activity": activity,
        "parsed_per_hour": parsed_per_hour,
        "parsed_per_day": parsed_per_day,
        "parsed_per_week": parsed_per_week,
        "next_cycle": next_cycle,
        "remaining_to_next_cycle": remaining_to_next_cycle,
        "eta_days": eta_days,
    }


def _stats_for_serial_refresh():
    """
    Статистика лёгкого обновления сериалов (vavada_serials.spawn_vavada_serials).
    Фильтрует только сериалы с уже найденным film_content в окне 8 лет.
    """
    today = timezone.now().date()
    now = timezone.now()
    refresh_days = 7
    year_window = 8
    current_year = today.year
    start_year = current_year - year_window
    cut_date = today - timedelta(days=refresh_days)

    base_qs = Content.objects.filter(
        is_serial=True,
        film_content__isnull=False,
        year_production__range=(start_year, current_year),
    )

    total = base_qs.count()
    waiting = base_qs.filter(
        last_update__lte=cut_date,
        is_parsed_ru="parsed",
    ).count()
    fresh = base_qs.filter(last_update__gt=cut_date).count()
    fresh_pct = (fresh / total * 100) if total else 0

    # Активность из ScraperLog (только успехи серийного refresh)
    activity = []
    for label, delta in [
        ("за час", timedelta(hours=1)),
        ("за сутки", timedelta(days=1)),
        ("за 7 дней", timedelta(days=7)),
        ("за 30 дней", timedelta(days=30)),
    ]:
        n = ScraperLog.objects.filter(
            task_name__startswith="Vavada serial refresh",
            status="success",
            created_at__gte=now - delta,
        ).count()
        activity.append({"label": label, "count": n})

    per_day = activity[1]["count"]
    eta_days = round(waiting / per_day, 1) if per_day > 0 else None

    return {
        "total": total,
        "waiting": waiting,
        "fresh": fresh,
        "fresh_pct": fresh_pct,
        "activity": activity,
        "per_day": per_day,
        "eta_days": eta_days,
        "refresh_days": refresh_days,
        "year_window": year_window,
    }


@staff_member_required
def parser_stats(request):
    """Дашборд состояния парсеров."""
    sources_meta = [
        {"source": "kp", "title": "Кинопоиск", "icon": "🎬"},
        {"source": "ru", "title": "Vavada (ru)", "icon": "📺"},
        {"source": "uz", "title": "Yangi.tv (uz)", "icon": "🇺🇿"},
    ]
    srcs = []
    for meta in sources_meta:
        data = _stats_for_source(meta["source"])
        data.update(meta)
        srcs.append(data)

    return render(
        request,
        "scrapers/parser_stats.html",
        {
            "srcs": srcs,
            "serial_refresh": _stats_for_serial_refresh(),
            "now": timezone.now(),
        },
    )
