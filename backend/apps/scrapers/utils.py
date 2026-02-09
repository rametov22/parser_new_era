import re


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
