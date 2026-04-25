from urllib.parse import urljoin

from ..kinopoisk_scrap_utils import normalize_film_href, load_page_and_soup


# award-search
def parse_awards(drivers, film_hrefs, additional_path):
    award_list = []
    try:
        film_hrefs = normalize_film_href(film_hrefs)
        href = urljoin(film_hrefs, additional_path)

        soup = load_page_and_soup(drivers, href)

        award_td = soup.find("td", style="padding-left: 20px")
        if not award_td:
            return award_list

        awards_tables = award_td.find_all("table", cellpadding="0")
        if not awards_tables:
            return award_list

        for award_table in awards_tables:

            # ---------- IMAGE ----------
            image = None
            img_td = award_table.find("td", align="center")
            if img_td:
                a = img_td.find("a")
                img = a.find("img") if a else None
                if img and img.get("src"):
                    src = img["src"]
                    if src.startswith("//"):
                        image = "https:" + src
                    elif src.startswith("http"):
                        image = src
                    else:
                        image = "https://" + src

            # ---------- AWARD INFO ----------
            award_name = None
            award_slug = None
            award_year = None

            awards_info = award_table.find("b")
            awards_info_a = awards_info.find("a") if awards_info else None

            if not awards_info_a or not awards_info_a.get("href"):
                continue

            href_parts = awards_info_a["href"].strip("/").split("/")
            if len(href_parts) >= 3:
                award_slug = href_parts[-2]
                award_year = href_parts[-1]

            award_name = awards_info_a.text.split(",")[0].strip()

            winner_content = []
            winner_participant = []
            nomination_content = []
            nomination_participant = []

            # ---------- WINNERS ----------
            winner_labels = award_table.find_all("font", color="#ff6600")
            for label in winner_labels:
                row = label.find_parent("tr")
                next_row = row.find_next_sibling("tr") if row else None
                td = next_row.find("td") if next_row else None
                ul = td.find("ul") if td else None
                if not ul:
                    continue

                for li in ul.find_all("li"):
                    li_text = li.get_text(strip=True)
                    if "(" in li_text and ")" in li_text:
                        winners_id = []
                        for a in li.find_all("a"):
                            href = a.get("href")
                            if href and "name/" in href:
                                winners_id.append(href.split("/")[2])
                        winner_participant.append(
                            {
                                "name": li_text.split("(")[0].strip(),
                                "winner_id": winners_id,
                            }
                        )
                    else:
                        winner_content.append(li_text)

            # ---------- NOMINATIONS ----------
            nomination_label = award_table.find("b", string="Номинации")
            if nomination_label:
                row = nomination_label.find_parent("tr")
                next_row = row.find_next_sibling("tr") if row else None
                td = next_row.find("td") if next_row else None
                ul = td.find("ul") if td else None

                if ul:
                    for li in ul.find_all("li"):
                        li_text = li.get_text(strip=True)
                        if "(" in li_text and ")" in li_text:
                            nomination_ids = []
                            for a in li.find_all("a"):
                                href = a.get("href")
                                if href and "name/" in href:
                                    nomination_ids.append(href.split("/")[2])
                            nomination_participant.append(
                                {
                                    "name": li_text.split("(")[0].strip(),
                                    "nomination_id": nomination_ids,
                                }
                            )
                        else:
                            nomination_content.append(li_text)

            award_list.append(
                {
                    "name": award_name,
                    "slug": award_slug,
                    "image": image,
                    "award_year": award_year,
                    "winner_content": winner_content,
                    "winner_participant": winner_participant,
                    "nomination_participant": nomination_participant,
                    "nomination_content": nomination_content,
                }
            )

    except Exception as ex:
        print("Ошибка при парсинге наград:", ex)

    return award_list
