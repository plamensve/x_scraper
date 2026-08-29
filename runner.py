from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import main as base

# Fuelo occasionally returns a lightweight/empty page on pagination requests.
# Retry each page before deciding that it is empty.
PAGE_RETRIES = 3
RETRY_DELAY_SECONDS = 3
NO_NEW_PAGE_LIMIT = 5
MAX_PAGE_INDEX = 1000

# Cities observed in Fuelo that were missing from the original static map.
# Keep these explicit so the region stored in Supabase stays compatible with
# the website's existing filters.
EXTRA_CITY_TO_REGION = {
    "Божурище": "София област",
    "Вакарел": "София област",
    "Златица": "София област",
    "Нови хан": "София област",
    "Равно поле": "София област",
}
base.CITY_TO_REGION.update(EXTRA_CITY_TO_REGION)

_original_normalize_fuel_name = base.normalize_fuel_name


def normalize_fuel_name(product):
    raw = base.normalize_spaces(product)
    if raw and raw.casefold() in {
        "газ",
        "газ lpg",
        "газ пропан бутан",
        "газ пропан-бутан",
        "propane gas",
    }:
        return "Пропан Бутан"
    return _original_normalize_fuel_name(product)


base.normalize_fuel_name = normalize_fuel_name


def page_fingerprint(cards):
    values = []
    for card in cards:
        station = base.extract_station_name(card) or ""
        city, location = base.extract_city_and_location(card)
        values.append((station, city or "", location or ""))
    return tuple(values)


def load_cards(driver, url, first_page=False):
    """Load one Fuelo page with retries and return its station cards."""
    last_html_size = 0
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            soup = base.get_soup(driver, url, first_page=first_page and attempt == 1)
            cards = base.find_station_cards(soup)
            last_html_size = len(str(soup))
            print(
                f"[PAGE] {url} | attempt={attempt}/{PAGE_RETRIES} | "
                f"station cards={len(cards)}"
            )
            if cards:
                return cards
        except Exception as exc:
            print(f"[PAGE] {url} | attempt={attempt}/{PAGE_RETRIES} failed: {exc}")

        if attempt < PAGE_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS * attempt)

    print(f"[PAGE] Giving up on {url}; last HTML size={last_html_size:,}")
    return []


def scrape_all_pages():
    """
    Scrape Fuelo using adaptive pagination.

    Older Fuelo URLs have behaved like offset pagination (/page/20, /page/40,
    ...), while deployments can also expose page-number pagination
    (/page/1, /page/2, ...). We probe both forms and continue with the one that
    actually returns a new page. This prevents a single empty /page/20 response
    from truncating the scrape after the first page.
    """
    all_results = []
    driver = base.create_driver()
    scraped_at = datetime.now(ZoneInfo("UTC")).isoformat()
    seen_fingerprints = set()

    def consume(cards, label):
        if not cards:
            return False
        fingerprint = page_fingerprint(cards)
        if not fingerprint or fingerprint in seen_fingerprints:
            print(f"[PAGE] {label} returned no new station page; skipping duplicate.")
            return False
        seen_fingerprints.add(fingerprint)
        before = len(all_results)
        for card in cards:
            all_results.extend(base.scrape_station_card(card, scraped_at))
        print(
            f"[TOTAL] {label} added {len(all_results) - before:,} rows; "
            f"collected so far: {len(all_results):,}"
        )
        return True

    try:
        first_url = base.make_page_url(0)
        first_cards = load_cards(driver, first_url, first_page=True)
        if not first_cards:
            raise RuntimeError("Fuelo first page returned no station cards after retries")
        consume(first_cards, "first page")

        # Probe Fuelo's offset-style pagination first because that is the form
        # historically used by this repository.
        offset_probe = base.PAGE_STEP
        offset_url = base.make_page_url(offset_probe)
        offset_cards = load_cards(driver, offset_url)

        if offset_cards and page_fingerprint(offset_cards) not in seen_fingerprints:
            mode = "offset"
            print(f"[PAGINATION] Detected offset mode; first next page={offset_probe}")
            consume(offset_cards, f"offset={offset_probe}")
            values = range(offset_probe + base.PAGE_STEP, base.MAX_OFFSET + base.PAGE_STEP, base.PAGE_STEP)
            make_url = base.make_page_url
        else:
            mode = "page-index"
            print("[PAGINATION] /page/20 did not yield a new page; probing page-index mode.")
            first_index = None
            first_index_cards = None
            for page_index in (1, 2, 3):
                url = f"{base.BASE_URL}/page/{page_index}?lang=bg"
                cards = load_cards(driver, url)
                fingerprint = page_fingerprint(cards) if cards else ()
                if cards and fingerprint not in seen_fingerprints:
                    first_index = page_index
                    first_index_cards = cards
                    break

            if first_index is None:
                print("[PAGINATION] No second unique Fuelo page detected; first page is the available result set.")
                values = []
                make_url = lambda value: ""
            else:
                print(f"[PAGINATION] Detected page-index mode; first next page={first_index}")
                consume(first_index_cards, f"page={first_index}")
                values = range(first_index + 1, MAX_PAGE_INDEX + 1)
                make_url = lambda value: f"{base.BASE_URL}/page/{value}?lang=bg"

        no_new_pages = 0
        for value in values:
            url = make_url(value)
            cards = load_cards(driver, url)
            if consume(cards, f"{mode}={value}"):
                no_new_pages = 0
            else:
                no_new_pages += 1
                if no_new_pages >= NO_NEW_PAGE_LIMIT:
                    print(
                        f"[STOP] {NO_NEW_PAGE_LIMIT} consecutive pages produced no new station page."
                    )
                    break
    finally:
        driver.quit()

    if not all_results:
        return pd.DataFrame(columns=base.COLUMNS)

    df = pd.DataFrame(all_results, columns=base.COLUMNS)
    df = df.drop_duplicates(
        subset=base.IN_RUN_DEDUP_COLUMNS,
        keep="last",
    ).reset_index(drop=True)
    return df


def prepare_for_supabase(df: pd.DataFrame) -> pd.DataFrame:
    """Prevent one unmapped city from failing the entire Supabase batch."""
    if df.empty:
        return df

    missing_region = df["region"].isna() | (df["region"].astype(str).str.strip() == "")
    if missing_region.any():
        missing_cities = sorted(
            str(value)
            for value in df.loc[missing_region, "city"].dropna().unique().tolist()
        )
        print(
            "[REGION] Skipping rows with unknown region instead of failing the whole run: "
            + ", ".join(missing_cities)
        )
        df = df.loc[~missing_region].copy()

    return df.reset_index(drop=True)


def main():
    df = scrape_all_pages()
    print(f"\nRows after in-run deduplication: {len(df):,}")

    # Keep the full debug CSV, including any row whose city still needs a new
    # region mapping. Only Supabase upload is filtered for schema safety.
    df.to_csv(base.OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"CSV saved: {base.OUTPUT_CSV}")

    if df.empty:
        raise RuntimeError("Scraper returned no rows; refusing to write to Supabase")

    upload_df = prepare_for_supabase(df)
    if upload_df.empty:
        raise RuntimeError("All scraped rows have unknown regions; refusing empty Supabase sync")

    supabase = base.get_supabase()
    new_df = base.remove_same_day_duplicates(supabase, upload_df)
    base.upload_to_supabase(supabase, new_df)


if __name__ == "__main__":
    main()
