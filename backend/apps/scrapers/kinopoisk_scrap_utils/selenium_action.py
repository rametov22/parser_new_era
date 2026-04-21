import time
import random
from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By


def click_button_square(drivers):
    try:
        square_button = WebDriverWait(drivers, 10).until(
            EC.element_to_be_clickable((By.ID, "js-button"))
        )
        square_button.click()
        drivers.switch_to.default_content()
    except Exception as cbs:
        print("Ошибка при нажатии кнопки", cbs)


def click_more_button(drivers):
    while True:
        try:
            show_more_button = WebDriverWait(drivers, 5).until(
                EC.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        "button[class^='styles_showMoreButton__']",
                    )
                )
            )
            show_more_button.click()

            time.sleep(1)
        except Exception:
            break


class ElementNotFoundException(Exception):
    pass


def scroll_until_find(drivers, scroll_height=1000, max_height=3000, timeout=20):
    end_time = time.time() + timeout
    current_height = 0

    while time.time() < end_time:
        drivers.execute_script(f"window.scrollBy(0, {scroll_height});")
        time.sleep(2)
        current_height += scroll_height

        try:
            element = drivers.find_element(By.CSS_SELECTOR, "div[data-tid='ea81b24f']")
            if element:
                return element
        except Exception:
            pass

        if current_height >= max_height:
            return None

    return None


def load_page_and_soup(drivers, url, wait_url=True, timeout=10):
    drivers.get(url)

    if "showcaptcha" in drivers.current_url:
        click_button_square(drivers)

    time.sleep(random.uniform(1, 2))

    if wait_url:
        WebDriverWait(drivers, timeout).until(EC.url_to_be(url))

    return BeautifulSoup(drivers.page_source, "lxml")
