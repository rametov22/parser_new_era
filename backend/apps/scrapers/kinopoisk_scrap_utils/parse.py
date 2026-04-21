import re
import datetime as dt


def parse_date(date_str, months):
    try:
        date_str = date_str.strip().lower()

        match = re.match(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", date_str)

        if not match:
            return None

        day, month_name, year = match.groups()

        month = months.get(month_name)
        if not month:
            return None

        formatted_date = f"{year}-{month}-{day.zfill(2)}"

        return dt.datetime.strptime(formatted_date, "%Y-%m-%d").date()

    except Exception as e:
        print(f"Ошибка преобразования даты: {date_str}, {e}")
        return None


def parse_people_block(about_film, test_id: str):
    people = []

    block = about_film.find("div", attrs={"data-test-id": test_id})
    if not block:
        return people

    for a in block.find_all("a", href=True):
        href = a.get("href")
        if "/name/" not in href:
            continue

        person_id = href.strip("/").split("/")[-1]
        people.append(
            {
                "id": int(person_id),
                "name": a.get_text(strip=True),
            }
        )

    return people


def parse_ru_date(raw_date, months_dict):
    if not raw_date:
        return None

    parts = raw_date.split()
    if len(parts) != 3:
        return None

    day, month_ru, year = parts
    month = months_dict.get(month_ru)
    if not month:
        return None

    try:
        return str(dt.date(int(year), month, int(day)))
    except ValueError:
        return None
