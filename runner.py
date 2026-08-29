from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import main as base

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# We prefer useful partial coverage over aggressive retries that can increase
# the chance of a Cloudflare challenge.
MAX_OFFSET = 1000
PAGE_STEP = 20

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


def safe_name(url):
    value = re.sub(r"^https?://", "", url)
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    return value[-150:]


def save_debug(driver, url, reason):
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}_{reason}_{safe_name(url)}"

    try:
        (DEBUG_DIR / f"{stem}.html").write_text(driver.page_source or "", encoding="utf-8")
    except Exception as exc:
        print(f"[DEBUG] Could not save HTML: {exc}")

    try:
        driver.save_screenshot(str(DEBUG_DIR / f"{stem}.png"))
    except Exception as exc:
        print(f"[DEBUG] Could not save screenshot: {exc}")

    try:
        (DEBUG_DIR / f"{stem}.txt").write_text(
            "\n".join(
                [
                    f"requested_url={url}",
                    f"current_url={driver.current_url}",
                    f"title={driver.title}",
                    f"reason={reason}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[DEBUG] Could not save metadata: {exc}")

    print(f"[DEBUG] Saved diagnostics for {url}")


def is_cloudflare_challenge(driver):
    title = (driver.title or "").strip().casefold()
    html = (driver.page_source or "").casefold()
    return (
        "just a moment" in title
        or "performing security verification" in html
        or "cf-chl-" in html
    )


def load_page(driver, url, first_page=False):
    """Load exactly once and classify the result without challenge retries."""
    try:
        soup = base.get_soup(driver, url, first_page=first_page)
    except Exception as exc:
        print(f"[PAGE] Load error for {url}: {exc}")
        save_debug(driver, url, "load_error")
        return [], "error"

    if is_cloudflare_challenge(driver):
        print(f"[CLOUDFLARE] Challenge detected at {url}; stopping pagination for this run.")
        save_debug(driver, url, "cloudflare")
        return [], "challenge"

    cards = base.find_station_cards(soup)
    print(f"[PAGE] {url} | station cards={len(cards)}")

    if not cards:
        save_debug(driver, url, "no_cards")
        return [], "empty"

    return cards, "ok"


def page_fingerprint(cards):
    values = []
    for card in cards:
        station = base.extract_station_name(card) or ""
        city, location = base.extract_city_and_location(card)
        values.append((station, city or "", location or ""))
    return tuple(values)


def scrape_best_effort():
    all_results = []
    seen_pages = set()
    driver = base.create_driver()
    scraped_at = datetime.now(ZoneInfo("UTC")).isoformat()

    def consume(cards, label):
        if not cards:
            return 0
        fingerprint = page_fingerprint(cards)
        if not fingerprint or fingerprint in seen_pages:
            print(f"[PAGE] {label} duplicates an already seen page; stopping pagination.")
            return -1
        seen_pages.add(fingerprint)

        before = len(all_results)
        for card in cards:
            all_results.extend(base.scrape_station_card(card, scraped_at))
        added = len(all_results) - before
        print(f"[TOTAL] {label} added {added:,} fuel rows; total={len(all_results):,}")
        return added

    try:
        first_url = base.make_page_url(0)
        first_cards, first_status = load_page(driver, first_url, first_page=True)

        if first_status != "ok":
            print(f"[BEST-EFFORT] First page unavailable ({first_status}); finishing without Supabase changes.")
            return pd.DataFrame(columns=base.COLUMNS)

        consume(first_cards, "first page")

        # Fuelo historically uses /page/20, /page/40, ... . Try each page once.
        # The first Cloudflare challenge or empty page ends pagination, while
        # preserving everything already collected from earlier pages.
        for offset in range(PAGE_STEP, MAX_OFFSET + PAGE_STEP, PAGE_STEP):
            url = base.make_page_url(offset)
            cards, status = load_page(driver, url)

            if status == "challenge":
                print(f"[BEST-EFFORT] Pagination stopped at offset={offset} because of Cloudflare.")
                break
            if status in {"empty", "error"}:
                print(f"[BEST-EFFORT] Pagination stopped at offset={offset} ({status}).")
                break

            if consume(cards, f"offset={offset}") == -1:
                break
    finally:
        driver.quit()

    if not all_results:
        return pd.DataFrame(columns=base.COLUMNS)

    df = pd.DataFrame(all_results, columns=base.COLUMNS)
    return df.drop_duplicates(
        subset=base.IN_RUN_DEDUP_COLUMNS,
        keep="last",
    ).reset_index(drop=True)


def prepare_for_supabase(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    missing_region = df["region"].isna() | (df["region"].astype(str).str.strip() == "")
    if missing_region.any():
        missing_cities = sorted(
            str(value)
            for value in df.loc[missing_region, "city"].dropna().unique().tolist()
        )
        print(
            "[REGION] Skipping rows with unknown region instead of failing the run: "
            + ", ".join(missing_cities)
        )
        df = df.loc[~missing_region].copy()

    return df.reset_index(drop=True)


def main():
    df = scrape_best_effort()
    print(f"\nRows after in-run deduplication: {len(df):,}")

    df.to_csv(base.OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"CSV saved: {base.OUTPUT_CSV}")

    # A run with no fuel rows is not a pipeline failure. The latest Fuelo page
    # can temporarily contain only EV stations or Cloudflare can challenge the
    # runner. In either case we leave Supabase untouched and try again later.
    if df.empty:
        print("[BEST-EFFORT] No fuel rows collected. Supabase left unchanged; run completes successfully.")
        return

    upload_df = prepare_for_supabase(df)
    if upload_df.empty:
        print("[BEST-EFFORT] No rows with known regions. Supabase left unchanged.")
        return

    supabase = base.get_supabase()
    new_df = base.remove_same_day_duplicates(supabase, upload_df)
    base.upload_to_supabase(supabase, new_df)


if __name__ == "__main__":
    main()
