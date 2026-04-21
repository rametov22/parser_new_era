def update_mains(
    content_obj,
    kino_poisk_rating,
    imdb_rating,
    age,
    sequel_list,
    short_description,
    trailer_link,
    is_serial,
    poster,
    premiere,
    premiere_ru,
    year_production,
    slogan,
    description,
    name_ru,
    name_original,
):
    changed_fields = []

    def check_and_update(field_name, new_value):
        old_value = getattr(content_obj, field_name)
        if not old_value or (new_value and old_value != new_value):
            setattr(content_obj, field_name, new_value)
            changed_fields.append(field_name)

    check_and_update("name_ru", name_ru)
    check_and_update("name_original", name_original)
    check_and_update("year_production", year_production)
    check_and_update("slogan", slogan)
    check_and_update("description", description)
    check_and_update("age_restriction", age)
    check_and_update("premiere", premiere)
    check_and_update("premiere_ru", premiere_ru)
    check_and_update("short_description", short_description)

    content_obj.kino_poisk_rating = kino_poisk_rating
    changed_fields.append("kino_poisk_rating")

    content_obj.imdb_rating = imdb_rating
    changed_fields.append("imdb_rating")

    if content_obj.is_serial != is_serial:
        content_obj.is_serial = is_serial
        changed_fields.append("is_serial")

    if not content_obj.poster_link:
        content_obj.poster_link = poster
        changed_fields.append("poster_link")

    if content_obj.trailer_link is None:
        content_obj.trailer_link = trailer_link
        changed_fields.append("trailer_link")

    if content_obj.additional is None:
        content_obj.additional = {}

    if content_obj.additional.get("sequel") != sequel_list:
        content_obj.additional["sequel"] = sequel_list
        changed_fields.append("additional")

    if changed_fields:
        content_obj.save(update_fields=changed_fields)


def save_serial_seasons(content_obj, seasons_dict, is_serial):
    if is_serial:
        content_obj.seasons = seasons_dict
        content_obj.save(update_fields=["seasons"])
