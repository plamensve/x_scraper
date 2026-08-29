import os
import re
import time
from decimal import Decimal, InvalidOperation

import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from supabase import create_client, Client

BASE_URL = "https://fuelo.net/prices/last_updated"
OUTPUT_CSV = "fuelo_all_prices.csv"
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "fuel_prices")

PAGE_STEP = 20
MAX_OFFSET = 10000
EMPTY_PAGE_LIMIT = 3
PAGE_DELAY_SECONDS = 1.0
INITIAL_LOAD_SECONDS = 3.0
HEADLESS = True

COLUMNS = ["created_at", "city", "station", "fuel", "price", "region", "location"]

# Exact duplicate observations inside the same scrape.
IN_RUN_DEDUP_COLUMNS = [
    "created_at", "city", "station", "fuel", "price", "region", "location"
]

# Identifies one fuel product at one physical station.
# created_at and price are deliberately excluded: they describe the station state,
# not the identity of the station/product.
STATION_FUEL_KEY_COLUMNS = ["city", "station", "fuel", "region", "location"]

REGION_CITIES = {
    "Благоевград": ["Благоевград", "Петрич", "Сандански", "Гоце Делчев", "Разлог", "Банско", "Симитли", "Кресна", "Хаджидимово", "Якоруда", "Кулата"],
    "Бургас": ["Бургас", "Айтос", "Карнобат", "Поморие", "Несебър", "Созопол", "Средец", "Царево", "Камено", "Приморско", "Малко Търново", "Обзор"],
    "Варна": ["Варна", "Провадия", "Девня", "Аксаково", "Белослав", "Дългопол", "Долни чифлик", "Суворово", "Вълчи дол"],
    "Велико Търново": ["Велико Търново", "Горна Оряховица", "Свищов", "Павликени", "Полски Тръмбеш", "Елена", "Лясковец", "Златарица", "Стражица", "Дебелец"],
    "Видин": ["Видин", "Белоградчик", "Дунавци", "Кула", "Димово", "Грамада", "Брегово", "Ружинци", "Макреш", "Ново село"],
    "Враца": ["Враца", "Козлодуй", "Мездра", "Бяла Слатина", "Оряхово", "Мизия", "Криводол", "Роман", "Хайредин", "Борован"],
    "Габрово": ["Габрово", "Севлиево", "Дряново", "Трявна", "Плачковци", "Градница", "Добромирка", "Шумата", "Кръвеник", "Стоките"],
    "Добрич": ["Добрич", "Балчик", "Генерал Тошево", "Каварна", "Тервел", "Шабла", "Крушари", "Албена", "Оброчище", "Кранево"],
    "Кърджали": ["Кърджали", "Момчилград", "Крумовград", "Ардино", "Джебел", "Кирково", "Черноочене", "Перперек", "Бенковски", "Фотиново"],
    "Кюстендил": ["Кюстендил", "Дупница", "Бобов дол", "Сапарева баня", "Рила", "Кочериново", "Бобошево", "Невестино", "Трекляно", "Ресилово"],
    "Ловеч": ["Ловеч", "Троян", "Тетевен", "Луковит", "Априлци", "Угърчин", "Ябланица", "Летница", "Орешак", "Гложене"],
    "Монтана": ["Монтана", "Лом", "Берковица", "Вършец", "Вълчедръм", "Бойчиновци", "Чипровци", "Медковец", "Якимово", "Брусарци"],
    "Пазарджик": ["Пазарджик", "Велинград", "Пещера", "Панагюрище", "Ракитово", "Септември", "Белово", "Брацигово", "Стрелча", "Батак"],
    "Перник": ["Перник", "Радомир", "Брезник", "Трън", "Земен", "Ковачевци", "Батановци", "Драгичево", "Рударци", "Дивотино"],
    "Плевен": ["Плевен", "Червен бряг", "Кнежа", "Левски", "Белене", "Долна Митрополия", "Пордим", "Гулянци", "Искър", "Славяново"],
    "Пловдив": ["Пловдив", "Асеновград", "Карлово", "Раковски", "Първомай", "Хисаря", "Сопот", "Стамболийски", "Куклен", "Радиново"],
    "Разград": ["Разград", "Исперих", "Кубрат", "Лозница", "Цар Калоян", "Завет", "Самуил", "Ясеновец", "Гецово", "Сеново"],
    "Русе": ["Русе", "Бяла", "Ветово", "Две могили", "Борово", "Сливо поле", "Иваново", "Ценово", "Мартен", "Николово"],
    "Силистра": ["Силистра", "Тутракан", "Дулово", "Главиница", "Алфатар", "Ситово", "Кайнарджа", "Айдемир", "Калипетрово", "Сребърна"],
    "Сливен": ["Сливен", "Нова Загора", "Котел", "Твърдица", "Шивачево", "Кермен", "Желю войвода", "Градец", "Сотиря"],
    "Смолян": ["Смолян", "Златоград", "Мадан", "Рудозем", "Девин", "Чепеларе", "Неделино", "Доспат", "Борино", "Пампорово"],
    "София": ["София"],
    "София област": ["Ботевград", "Самоков", "Своге", "Елин Пелин", "Костинброд", "Ихтиман", "Пирдоп", "Сливница", "Правец", "Копривщица"],
    "Стара Загора": ["Стара Загора", "Казанлък", "Чирпан", "Раднево", "Гълъбово", "Мъглиж", "Гурково", "Николаево", "Шипка", "Павел баня"],
    "Търговище": ["Търговище", "Попово", "Омуртаг", "Антоново", "Опака", "Стража", "Дралфа", "Подгорица", "Макариополско"],
    "Хасково": ["Хасково", "Димитровград", "Свиленград", "Харманли", "Любимец", "Ивайловград", "Симеоновград", "Тополовград", "Минерални бани", "Меричлери"],
    "Шумен": ["Шумен", "Нови пазар", "Велики Преслав", "Смядово", "Каспичан", "Каолиново", "Плиска", "Върбица", "Хитрино", "Венец"],
    "Ямбол": ["Ямбол", "Елхово", "Стралджа", "Болярово", "Кукорево", "Веселиново", "Безмер", "Калчево", "Роза"],
}

CITY_TO_REGION = {city: region for region, cities in REGION_CITIES.items() for city in cities}

STATION_BRANDS = [
    ("omv", "OMV"), ("shell", "Shell"), ("lukoil", "Lukoil"), ("лукойл", "Lukoil"),
    ("eko", "ЕКО"), ("еко", "ЕКО"), ("rompetrol", "Rompetrol"), ("ромпетрол", "Rompetrol"),
    ("petrol", "Petrol"), ("петрол", "Petrol"), ("avia", "AVIA"), ("инса ойл", "Insa Oil"),
    ("insa oil", "Insa Oil"), ("gazprom", "Gazprom"), ("газпром", "Gazprom"),
    ("cruise", "Cruise"), ("круиз", "Cruise"), ("dieselor", "Dieselor"), ("зара", "Зара"),
    ("топливо", "Топливо"),
]


def normalize_spaces(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def get_region_by_city(city):
    city = normalize_spaces(city)
    if not city:
        return None
    region = CITY_TO_REGION.get(city)
    if not region:
        print(f"[REGION] Missing mapping for city: {city}")
    return region


def create_driver():
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    return driver


def get_soup(driver, url, first_page=False):
    driver.get(url)
    time.sleep(INITIAL_LOAD_SECONDS if first_page else PAGE_DELAY_SECONDS)
    html = driver.page_source
    print(f"[PAGE] {url} | HTML: {len(html):,} chars")
    return BeautifulSoup(html, "html.parser")


def make_page_url(offset):
    return f"{BASE_URL}?lang=bg" if offset == 0 else f"{BASE_URL}/page/{offset}?lang=bg"


def clean_price(price_text):
    price_text = normalize_spaces(price_text)
    if not price_text:
        raise ValueError("Empty price")
    match = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,3})?)(?!\d)", price_text)
    if not match:
        raise ValueError(f"No numeric price found in: {price_text}")
    return float(match.group(1).replace(",", "."))


def normalize_fuel_name(product):
    raw = normalize_spaces(product)
    if not raw:
        return None
    p = raw.casefold()

    if any(x in p for x in ["електр", "electric", "kwh", "kw"]):
        return None
    if any(x in p for x in ["lpg", "autogas", "автогаз", "пропан", "бутан", "blue force gas"]):
        return "Пропан Бутан"
    if any(x in p for x in ["метан", "methane", "cng"]):
        return "Метан"
    if any(x in p for x in ["diesel", "дизел", "нафта"]):
        premium_tokens = [
            "premium", "премиум", "maxxmotion", "ecto", "v-power",
            "double filtered", "green force", "diesel+", "diesel +",
            "topdiesel", "топдизел",
        ]
        return "Дизел премиум" if any(x in p for x in premium_tokens) else "Дизел"
    if re.search(r"(?<!\d)100(?!\d)", p):
        return "Бензин A100"
    if re.search(r"(?<!\d)98(?!\d)", p):
        return "Бензин A98"
    if re.search(r"(?<!\d)95(?!\d)", p) or any(x in p for x in ["a95", "а95", "a-95", "а-95", "super 95"]):
        return "Бензин A95"
    if any(x in p for x in ["бензин", "gasoline", "petrol"]):
        return "Бензин"

    print(f"[FUEL] Unmapped product: {raw}")
    return None


def parse_created_at(card_text):
    card_text = normalize_spaces(card_text) or ""
    now_sofia = datetime.now(ZoneInfo("Europe/Sofia"))

    absolute = re.search(
        r"(?:последно\s+обновяване|обновено|актуализирано)?\s*"
        r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
        card_text,
        flags=re.IGNORECASE,
    )
    if absolute:
        local_dt = datetime.strptime(
            f"{absolute.group(1)} {absolute.group(2)}", "%d.%m.%Y %H:%M"
        ).replace(tzinfo=ZoneInfo("Europe/Sofia"))
        return local_dt.astimezone(ZoneInfo("UTC")).isoformat()

    today_match = re.search(r"днес\s+(?:в\s+)?(\d{1,2}:\d{2})", card_text, flags=re.IGNORECASE)
    if today_match:
        hour, minute = map(int, today_match.group(1).split(":"))
        local_dt = now_sofia.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return local_dt.astimezone(ZoneInfo("UTC")).isoformat()

    return now_sofia.astimezone(ZoneInfo("UTC")).isoformat()


def extract_station_name(card):
    h1_link = card.select_one("h1 a")
    return normalize_spaces(h1_link.get_text(" ", strip=True)) if h1_link else None


def normalize_station_name(station_name, city=None):
    station_name = normalize_spaces(station_name)
    if not station_name:
        return None

    normalized = station_name.casefold()
    for prefix, canonical in STATION_BRANDS:
        if normalized.startswith(prefix.casefold()):
            return canonical

    fallback = re.sub(r"^бензиностанция\s+", "", station_name, flags=re.IGNORECASE).strip()
    city = normalize_spaces(city)
    if city:
        fallback = re.sub(
            rf"\s*[-,:]?\s*{re.escape(city)}\s*$", "", fallback, flags=re.IGNORECASE
        ).strip()
    return fallback or station_name


def extract_city_and_location(card):
    h4 = card.select_one("h4")
    if not h4:
        return None, None

    city_element = h4.select_one("a")
    if not city_element:
        return None, normalize_spaces(h4.get_text(" ", strip=True))

    city = normalize_spaces(city_element.get_text(" ", strip=True))
    location = normalize_spaces(h4.get_text(" ", strip=True))
    if city and location:
        location = re.sub(
            rf"^\s*{re.escape(city)}\s*[,\-:]?\s*",
            "",
            location,
            count=1,
            flags=re.IGNORECASE,
        )
        location = location.replace('"', "").strip(" ,")

    return city, location


def find_station_cards(soup):
    return [
        card for card in soup.select("div.row")
        if card.select_one("h1 a") and card.select_one("h4") and card.select_one("table.table")
    ]


def scrape_station_card(card):
    results = []
    station_title = extract_station_name(card)
    if not station_title:
        return results

    city, location = extract_city_and_location(card)
    region = get_region_by_city(city)
    station = normalize_station_name(station_title, city=city)
    created_at = parse_created_at(card.get_text(" ", strip=True))
    table = card.select_one("table.table")
    if not table:
        return results

    for row in table.select("tbody tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue

        product_raw = normalize_spaces(cells[1].get_text(" ", strip=True))
        price_raw = normalize_spaces(cells[2].get_text(" ", strip=True))
        if not product_raw or not price_raw:
            continue

        fuel = normalize_fuel_name(product_raw)
        if not fuel:
            continue

        try:
            price = clean_price(price_raw)
        except ValueError as exc:
            print(f"[PRICE] {station_title} | {product_raw} | {exc}")
            continue

        results.append({
            "created_at": created_at,
            "city": city,
            "station": station,
            "fuel": fuel,
            "price": price,
            "region": region,
            "location": location,
        })

    print(f"[STATION] {station_title} | {city or '-'} | {len(results)} fuel prices")
    return results


def scrape_all_pages():
    all_results = []
    empty_pages = 0
    driver = create_driver()

    try:
        for offset in range(0, MAX_OFFSET + PAGE_STEP, PAGE_STEP):
            url = make_page_url(offset)
            try:
                soup = get_soup(driver, url, first_page=(offset == 0))
            except Exception as exc:
                print(f"[ERROR] Could not load {url}: {exc}")
                empty_pages += 1
                if empty_pages >= EMPTY_PAGE_LIMIT:
                    print("[STOP] Too many consecutive failed/empty pages.")
                    break
                continue

            cards = find_station_cards(soup)
            print(f"[PAGE] offset={offset} | station cards={len(cards)}")

            if not cards:
                empty_pages += 1
                if empty_pages >= EMPTY_PAGE_LIMIT:
                    print(f"[STOP] {EMPTY_PAGE_LIMIT} consecutive pages without station cards.")
                    break
                continue

            empty_pages = 0
            for card in cards:
                all_results.extend(scrape_station_card(card))
            print(f"[TOTAL] rows collected so far: {len(all_results):,}")
    finally:
        driver.quit()

    if not all_results:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(all_results, columns=COLUMNS)
    df = df.drop_duplicates(subset=IN_RUN_DEDUP_COLUMNS).reset_index(drop=True)
    return df


def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables"
        )
    return create_client(url, key)


def canonical_text(value):
    if value is None:
        return ""
    return normalize_spaces(value) or ""


def station_fuel_key(row):
    return tuple(canonical_text(row.get(col)) for col in STATION_FUEL_KEY_COLUMNS)


def canonical_price(value):
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_latest_prices(client: Client):
    """Return the latest known price for every station/fuel identity in Supabase."""
    latest_by_key = {}
    start = 0
    page_size = 1000
    select_columns = ["created_at", "price", *STATION_FUEL_KEY_COLUMNS]

    while True:
        response = (
            client.table(SUPABASE_TABLE)
            .select(",".join(select_columns))
            .order("created_at", desc=True)
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = response.data or []

        # Because rows arrive newest-first, the first row seen for a key is its
        # current/most recent database state.
        for row in batch:
            key = station_fuel_key(row)
            if key not in latest_by_key:
                latest_by_key[key] = canonical_price(row.get("price"))

        if len(batch) < page_size:
            break
        start += page_size

    print(f"[SUPABASE] Current station/fuel states loaded: {len(latest_by_key):,}")
    return latest_by_key


def keep_latest_scraped_state(df: pd.DataFrame):
    """Keep only the newest scraped observation per station/fuel identity."""
    if df.empty:
        return df

    work = df.copy()
    work["_parsed_created_at"] = pd.to_datetime(work["created_at"], utc=True, errors="coerce")
    work = work.sort_values("_parsed_created_at", kind="stable")
    work = work.drop_duplicates(subset=STATION_FUEL_KEY_COLUMNS, keep="last")
    work = work.drop(columns=["_parsed_created_at"])
    return work.reset_index(drop=True)


def remove_unchanged_prices(client: Client, df: pd.DataFrame):
    """
    Insert a row only when:
      * this station/fuel does not exist yet in Supabase, or
      * its newly scraped price differs from the latest stored price.

    Therefore a price that changes 1.60 -> 1.62 -> 1.60 is preserved as three
    historical changes, while repeated 1.60 scrapes are not stored again.
    """
    if df.empty:
        return df

    latest_prices = fetch_latest_prices(client)
    df = keep_latest_scraped_state(df)
    records = df.where(pd.notnull(df), None).to_dict(orient="records")

    changed_records = []
    unchanged_count = 0

    for row in records:
        key = station_fuel_key(row)
        new_price = canonical_price(row.get("price"))
        previous_price = latest_prices.get(key)

        if key not in latest_prices or new_price != previous_price:
            changed_records.append(row)
            latest_prices[key] = new_price
        else:
            unchanged_count += 1

    print(f"[DEDUP] Latest scraped station/fuel states: {len(records):,}")
    print(f"[DEDUP] Unchanged prices skipped: {unchanged_count:,}")
    print(f"[DEDUP] New/changed prices to insert: {len(changed_records):,}")

    return pd.DataFrame(changed_records, columns=COLUMNS)


def upload_to_supabase(client: Client, df: pd.DataFrame):
    if df.empty:
        print("[SUPABASE] Nothing new or changed to insert.")
        return

    records = df.where(pd.notnull(df), None).to_dict(orient="records")
    batch_size = 500

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        client.table(SUPABASE_TABLE).insert(batch).execute()
        print(f"[SUPABASE] Inserted {min(i + batch_size, len(records)):,}/{len(records):,}")


if __name__ == "__main__":
    df = scrape_all_pages()
    print(f"\nRows after in-run exact deduplication: {len(df):,}")

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"CSV saved: {OUTPUT_CSV}")

    if df.empty:
        raise RuntimeError("Scraper returned no rows; refusing to write to Supabase")

    supabase = get_supabase()
    changed_df = remove_unchanged_prices(supabase, df)
    upload_to_supabase(supabase, changed_df)
