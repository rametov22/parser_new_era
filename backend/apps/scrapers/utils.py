import re
from io import BytesIO

import requests
from django.core.files.base import ContentFile
from PIL import Image


def download_image_to_field(file_field, image_url, name_base, timeout=15):
    """
    Скачивает изображение по URL и сохраняет в указанное FileField/ImageField
    через storage (MinIO). Делает .save(save=True) — модель сохранится сразу.

    file_field: FieldFile, например content_obj.poster_uz
    image_url:  URL картинки
    name_base:  базовое имя файла без расширения (например, kino_poisk_id)

    Возвращает True если успех, False если нет.
    """
    if not image_url:
        return False

    try:
        response = requests.get(image_url, timeout=timeout)
        if response.status_code != 200:
            print(f"[image] {image_url} -> {response.status_code}, пропускаем")
            return False

        image_bytes = response.content
        image = Image.open(BytesIO(image_bytes))
        image.verify()
        image = Image.open(BytesIO(image_bytes))  # переоткрытие после verify

        image_extension = {
            "JPEG": "jpg",
            "PNG": "png",
            "GIF": "gif",
            "WEBP": "webp",
        }.get(image.format, "jpg")

        image_name = f"{name_base}.{image_extension}"
        file_field.save(image_name, ContentFile(image_bytes), save=True)
        return True

    except Exception as e:
        print(
            f"[image] ОШИБКА скачивания {image_url}: "
            f"{type(e).__name__}: {e}"
        )
        return False


def parse_age(age: str | None) -> int | None:
    if not age:
        return None
    match = re.search(r"\d+", age)
    return int(match.group()) if match else None


def parse_episode_string(episode_str):
    """
    Извлекает цифры из строки типа '2-fasl 7-qism' или '1-fasl 120-qism'
    Возвращает кортеж (season, episode)
    """
    if not episode_str:
        return None, None

    # Ищем все числа в строке
    numbers = re.findall(r"\d+", episode_str)

    try:
        if len(numbers) >= 2:
            return int(numbers[0]), int(numbers[1])
        elif len(numbers) == 1:
            # Если только одно число, решаем: это серия или сезон?
            # Обычно в таких строках это серия.
            return 1, int(numbers[0])
    except ValueError:
        pass

    return None, None
