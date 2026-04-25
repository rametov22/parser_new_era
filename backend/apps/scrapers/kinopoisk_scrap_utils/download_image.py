import requests
from django.core.files.base import ContentFile
import os
from PIL import Image
from io import BytesIO


def download_and_save_poster(content_obj, poster_url):
    if not poster_url:
        print(f"[poster] пустой poster_url для kp_id={content_obj.kino_poisk_id}")
        return

    try:
        response = requests.get(poster_url, timeout=10)
        print(f"[poster] GET {poster_url} -> {response.status_code}")
        if response.status_code == 200:
            image_bytes = response.content
            image = Image.open(BytesIO(image_bytes))
            image.verify()

            image = Image.open(BytesIO(image_bytes))

            image_extension = {
                "JPEG": "jpg",
                "PNG": "png",
                "GIF": "gif",
                "WEBP": "webp",
            }.get(image.format, "jpg")

            image_name = f"{content_obj.kino_poisk_id}.{image_extension}"

            content_obj.poster.save(image_name, ContentFile(image_bytes), save=True)
            print(f"[poster] сохранён {image_name} ({len(image_bytes)} байт)")
        else:
            print(f"[poster] неожиданный статус {response.status_code}, постер не сохранён")
    except Exception as e:
        print(f"[poster] ОШИБКА для kp_id={content_obj.kino_poisk_id}: {type(e).__name__}: {e}")


def save_image_from_url_to_award(content_obj, poster_url):
    if not poster_url:
        return

    try:
        response = requests.get(poster_url, timeout=10)
        if response.status_code == 200:
            image_bytes = response.content
            image = Image.open(BytesIO(image_bytes))
            image.verify()

            image = Image.open(BytesIO(image_bytes))

            image_extension = {
                "JPEG": "jpg",
                "PNG": "png",
                "GIF": "gif",
                "WEBP": "webp",
            }.get(image.format, "jpg")

            image_name = f"{poster_url.split('/')[-1]}.{image_extension}"

            content_obj.image.save(image_name, ContentFile(image_bytes), save=True)
    except Exception as e:
        print(f"[award-image] ОШИБКА для {poster_url}: {type(e).__name__}: {e}")
