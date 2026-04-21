from .. import models


def save_country(content_obj, country_list):
    for country_data in country_list:
        country, created = models.Country.objects.get_or_create(
            country_id=country_data["id"], defaults={"name": country_data["name"]}
        )
        content_obj.countries.add(country)


def save_genre(content_obj, genre_list):
    for genre_data in genre_list:
        genre, created = models.Genre.objects.get_or_create(
            slug=genre_data["slug"], defaults={"name": genre_data["name"]}
        )
        content_obj.genres.add(genre)


def save_participants(
    content_obj,
    directors_list,
    screenwriters_list,
    producers_list,
    operators_list,
    composers_list,
    editors_list,
):
    director_ordering = 1
    #
    for director_data in directors_list:
        director, created = models.Participant.objects.get_or_create(
            participant_id=director_data["id"], defaults={"name": director_data["name"]}
        )
        models.ContentDirector.objects.update_or_create(
            content=content_obj,
            participant=director,
            defaults={"ordering": director_ordering},
        )
        director_ordering += 1
    #
    screenwriter_ordering = 1
    for screenwriter_data in screenwriters_list:
        screenwriter, created = models.Participant.objects.get_or_create(
            participant_id=screenwriter_data["id"],
            defaults={"name": screenwriter_data["name"]},
        )
        models.ContentScreenwriter.objects.update_or_create(
            content=content_obj,
            participant=screenwriter,
            defaults={"ordering": screenwriter_ordering},
        )
        screenwriter_ordering += 1
    #
    producer_ordering = 1
    for producer_data in producers_list:
        producer, created = models.Participant.objects.get_or_create(
            participant_id=producer_data["id"], defaults={"name": producer_data["name"]}
        )
        models.ContentProducer.objects.update_or_create(
            content=content_obj,
            participant=producer,
            defaults={"ordering": producer_ordering},
        )
        producer_ordering += 1
    #
    operator_ordering = 1
    for operator_data in operators_list:
        operator, created = models.Participant.objects.get_or_create(
            participant_id=operator_data["id"], defaults={"name": operator_data["name"]}
        )
        models.ContentOperator.objects.update_or_create(
            content=content_obj,
            participant=operator,
            defaults={"ordering": operator_ordering},
        )
        operator_ordering += 1
    #
    composer_ordering = 1
    for composer_data in composers_list:
        composer, created = models.Participant.objects.get_or_create(
            participant_id=composer_data["id"], defaults={"name": composer_data["name"]}
        )
        models.ContentComposer.objects.update_or_create(
            content=content_obj,
            participant=composer,
            defaults={"ordering": composer_ordering},
        )
        composer_ordering += 1
    #
    editor_ordering = 1
    for editor_data in editors_list:
        editor, created = models.Participant.objects.get_or_create(
            participant_id=editor_data["id"], defaults={"name": editor_data["name"]}
        )
        models.ContentEditor.objects.update_or_create(
            content=content_obj,
            participant=editor,
            defaults={"ordering": editor_ordering},
        )
        editor_ordering += 1


def save_collections(content_obj, collections_list):
    for collection_data in collections_list:
        collection, created = models.Collection.objects.get_or_create(
            slug=collection_data["slug"], defaults={"name": collection_data["name"]}
        )
        content_obj.collections.add(collection)


def save_actors(content_obj, actors_list):
    actor_ordering = 1
    for actor_data in actors_list:
        actor, created = models.Participant.objects.get_or_create(
            participant_id=actor_data["id"], defaults={"name": actor_data["name"]}
        )
        models.ContentActor.objects.update_or_create(
            content=content_obj,
            participant=actor,
            defaults={"ordering": actor_ordering, "role": actor_data["role"]},
        )
        actor_ordering += 1


def save_keywords(content_obj, keyword_list):
    for keyword_data in keyword_list:
        keyword, created = models.Keyword.objects.get_or_create(
            keyword_id=keyword_data["id"], defaults={"name": keyword_data["name"]}
        )
        content_obj.keywords.add(keyword)


def save_platform(content_obj, platform_obj):
    if platform_obj is None:
        return
    content_obj.platform = platform_obj
    content_obj.save(update_fields=["platform"])


def save_studio(content_obj, studio_list):
    for studio_data in studio_list:
        studio, created = models.Studio.objects.get_or_create(
            studio_id=studio_data["id"], defaults={"name": studio_data["name"]}
        )
        content_obj.studios.add(studio)


def save_like(content_obj, like_list):
    if content_obj.additional is None:
        content_obj.additional = {}
    content_obj.additional["recs"] = like_list

    content_obj.save(update_fields=["additional"])
