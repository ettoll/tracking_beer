"""
Daily scraper for DBMF holdings from IMGP.

This script:
1. Downloads the DBMF fund page.
2. Extracts the holdings table into data/total_data_positions.csv.
3. Extracts fund-level information into data/total_data_descriptions.csv.
4. Appends only new rows and avoids duplicates.

Run locally from the repo root with:
    python tracking/fetch_dbmf.py
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup


URL = "https://www.imgp.com/us/fund/us53700t8273-imgp-dbi-managed-futures-strategy-etf/"

# Repo root = one folder above /tracking
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

POSITIONS_PATH = DATA_DIR / "total_data_positions.csv"
DESCRIPTIONS_PATH = DATA_DIR / "total_data_descriptions.csv"


# Final column order used in your existing CSVs
POSITIONS_COLUMNS = [
    "DATE",
    "CUSIP",
    "TICKER",
    "DESCRIPTION",
    "SHARES",
    "BASE_MV",
    "PCT_HOLDINGS",
]

DESCRIPTIONS_COLUMNS = [
    "DATE",
    "NAV",
    "SHARES_OUTSTANDING",
    "TOTAL_NET_ASSETS",
    "TOTAL_EXPENSE_RATIO",
]


def download_html(url: str = URL) -> str:
    """Download page HTML with browser-like headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    html = response.text

    if "scheduled maintenance" in html.lower():
        raise RuntimeError(
            "The IMGP website currently shows 'Scheduled Maintenance'. "
            "Try again later or let the GitHub Action run tomorrow."
        )

    return html


def normalize_col_name(col: object) -> str:
    """Convert table column names to stable snake-case style names."""
    col = str(col)
    col = re.sub(r"\s+", " ", col).strip().lower()
    col = col.replace("%", " pct ")
    col = re.sub(r"[^a-z0-9]+", "_", col)
    col = re.sub(r"_+", "_", col).strip("_")
    return col


def clean_text(value: object) -> Optional[str]:
    if pd.isna(value):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if text else None


def clean_number(value: object) -> Optional[float]:
    """
    Convert values like '$1,234.50', '(123.4)', '3.1B', '2.4M', '0.85%'
    into floats. Percent conversion itself is handled in clean_percent.
    """
    if pd.isna(value):
        return None

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-", "—", "n/a", "na"}:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = text.replace("$", "").replace(",", "").replace("%", "").strip()

    multiplier = 1.0
    if text.lower().endswith("b"):
        multiplier = 1_000_000_000.0
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.lower().endswith("k"):
        multiplier = 1_000.0
        text = text[:-1]

    text = re.sub(r"[^0-9.\-]", "", text)

    if text in {"", "-", "."}:
        return None

    number = float(text) * multiplier
    return -number if negative else number


def clean_percent(value: object) -> Optional[float]:
    """
    Convert '53%' to 0.53.
    If the website already returns 0.53, keep it as 0.53.
    """
    if pd.isna(value):
        return None

    raw = str(value)
    number = clean_number(raw)
    if number is None:
        return None

    if "%" in raw:
        return number / 100.0

    # Safety: if a value looks like 53 rather than 0.53, convert to 0.53.
    if abs(number) > 5:
        return number / 100.0

    return number


def parse_date(value: object) -> Optional[int]:
    """Return date as YYYYMMDD integer."""
    if pd.isna(value):
        return None

    text = str(value).strip()

    # Already like 20260306
    if re.fullmatch(r"\d{8}", text):
        return int(text)

    dt = pd.to_datetime(text, errors="coerce")
    if pd.isna(dt):
        return None

    return int(dt.strftime("%Y%m%d"))


def find_as_of_date(html: str) -> Optional[int]:
    """Find a page-level 'as of' date if the holdings table itself has no date column."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    patterns = [
        r"as\s+of\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"as\s+of\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"as\s+of\s+(\d{4}-\d{2}-\d{2})",
        r"Date\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"Date\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"Date\s+(\d{4}-\d{2}-\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed

    return None


def first_matching_column(columns: list[str], patterns: list[str]) -> Optional[str]:
    """Return first column that matches any regex pattern."""
    for pattern in patterns:
        for col in columns:
            if re.search(pattern, col, flags=re.IGNORECASE):
                return col
    return None


def read_html_tables(html: str) -> list[pd.DataFrame]:
    """Read all HTML tables and normalize their column names."""
    tables = pd.read_html(StringIO(html))
    normalized_tables = []

    for table in tables:
        table = table.copy()
        table.columns = [normalize_col_name(c) for c in table.columns]
        normalized_tables.append(table)

    return normalized_tables


def parse_positions(html: str) -> pd.DataFrame:
    """Extract the holdings/positions table."""
    tables = read_html_tables(html)
    as_of_date = find_as_of_date(html)

    best_table = None

    for table in tables:
        cols = list(table.columns)
        has_cusip = any("cusip" in c for c in cols)
        has_security = any(("security" in c or "description" in c or "holding" in c or "name" in c) for c in cols)
        has_value = any(("market" in c or "value" in c or "base_mv" in c) for c in cols)
        has_weight = any(("weight" in c or "pct" in c or "holdings" in c) for c in cols)

        if has_cusip and (has_security or has_value or has_weight):
            best_table = table
            break

    if best_table is None:
        raise RuntimeError(
            "Could not find the holdings table in the IMGP page. "
            "The website structure may have changed."
        )

    table = best_table.copy()
    cols = list(table.columns)

    col_date = first_matching_column(cols, [r"^date$", r"as_of"])
    col_cusip = first_matching_column(cols, [r"cusip"])
    col_ticker = first_matching_column(cols, [r"ticker", r"symbol"])
    col_description = first_matching_column(
        cols,
        [r"security_name", r"description", r"holding", r"security", r"name"],
    )
    col_shares = first_matching_column(cols, [r"shares", r"quantity", r"notional"])
    col_base_mv = first_matching_column(cols, [r"base_mv", r"market_value", r"market", r"value"])
    col_pct = first_matching_column(cols, [r"pct_holdings", r"weight", r"pct", r"percent", r"holdings"])

    required = {
        "CUSIP": col_cusip,
        "DESCRIPTION": col_description,
        "SHARES": col_shares,
        "BASE_MV": col_base_mv,
        "PCT_HOLDINGS": col_pct,
    }

    missing = [name for name, col in required.items() if col is None]
    if missing:
        raise RuntimeError(
            "Could not map these required holdings columns: "
            + ", ".join(missing)
            + f". Website columns found: {cols}"
        )

    result = pd.DataFrame()
    if col_date is not None:
        result["DATE"] = table[col_date].apply(parse_date)
    else:
        if as_of_date is None:
            raise RuntimeError("Could not find a DATE/as-of date for the holdings table.")
        result["DATE"] = as_of_date

    result["CUSIP"] = table[col_cusip].apply(clean_text)
    result["TICKER"] = table[col_ticker].apply(clean_text) if col_ticker else pd.NA
    result["DESCRIPTION"] = table[col_description].apply(clean_text)
    result["SHARES"] = table[col_shares].apply(clean_number)
    result["BASE_MV"] = table[col_base_mv].apply(clean_number)
    result["PCT_HOLDINGS"] = table[col_pct].apply(clean_percent)

    result = result[POSITIONS_COLUMNS]
    result = result.dropna(subset=["DATE", "CUSIP", "DESCRIPTION"])
    result["DATE"] = result["DATE"].astype(int)

    return result


def extract_label_value_from_text(text: str, labels: list[str]) -> Optional[float]:
    """
    Search page text for a numeric value after one of the labels.
    Example: 'NAV $30.25' or 'Total Net Assets $3.1B'.
    """
    for label in labels:
        pattern = rf"{label}\s*[:\-]?\s*(\$?\(?-?[\d,]+(?:\.\d+)?\)?\s*[%KMBkmb]?)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_number(match.group(1))

    return None


def parse_descriptions(html: str) -> pd.DataFrame:
    """Extract fund-level data such as NAV, shares outstanding and total net assets."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    as_of_date = find_as_of_date(html)

    if as_of_date is None:
        raise RuntimeError("Could not find a DATE/as-of date for the fund description data.")

    nav = extract_label_value_from_text(text, [
        r"NAV",
        r"Net\s+Asset\s+Value",
    ])

    shares_outstanding = extract_label_value_from_text(text, [
        r"Shares\s+Outstanding",
    ])

    total_net_assets = extract_label_value_from_text(text, [
        r"Total\s+Net\s+Assets",
        r"Net\s+Assets",
    ])

    expense_ratio = extract_label_value_from_text(text, [
        r"Total\s+Expense\s+Ratio",
        r"Gross\s+Expense\s+Ratio",
        r"Expense\s+Ratio",
    ])

    # Expense ratio appears as 0.85% on many fund pages; store as 0.0085.
    if expense_ratio is not None and expense_ratio > 0.05:
        # If parsed from '0.85%' clean_number returns 0.85.
        expense_ratio = expense_ratio / 100.0

    row = {
        "DATE": as_of_date,
        "NAV": nav,
        "SHARES_OUTSTANDING": shares_outstanding,
        "TOTAL_NET_ASSETS": total_net_assets,
        "TOTAL_EXPENSE_RATIO": expense_ratio,
    }

    result = pd.DataFrame([row], columns=DESCRIPTIONS_COLUMNS)

    # If the page did not expose all values, still save the date and whatever was found.
    # But require at least one real fund-level metric besides DATE.
    if result[["NAV", "SHARES_OUTSTANDING", "TOTAL_NET_ASSETS", "TOTAL_EXPENSE_RATIO"]].isna().all(axis=None):
        raise RuntimeError(
            "Could not extract NAV / shares outstanding / net assets / expense ratio. "
            "The website structure may have changed."
        )

    return result


def append_without_duplicates(
    new_data: pd.DataFrame,
    path: Path,
    columns: list[str],
    subset: list[str],
) -> pd.DataFrame:
    """Append new_data to existing CSV and remove duplicates."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        old_data = pd.read_csv(path)
        combined = pd.concat([old_data, new_data], ignore_index=True)
    else:
        combined = new_data.copy()

    for col in columns:
        if col not in combined.columns:
            combined[col] = pd.NA

    combined = combined[columns]
    combined = combined.drop_duplicates(subset=subset, keep="last")
    combined = combined.sort_values(columns[0]).reset_index(drop=True)

    combined.to_csv(path, index=False)
    return combined


def main() -> None:
    html = download_html()

    new_positions = parse_positions(html)
    new_descriptions = parse_descriptions(html)

    positions = append_without_duplicates(
        new_data=new_positions,
        path=POSITIONS_PATH,
        columns=POSITIONS_COLUMNS,
        subset=["DATE", "CUSIP", "TICKER", "DESCRIPTION"],
    )

    descriptions = append_without_duplicates(
        new_data=new_descriptions,
        path=DESCRIPTIONS_PATH,
        columns=DESCRIPTIONS_COLUMNS,
        subset=["DATE"],
    )

    latest_date = new_positions["DATE"].iloc[0]
    print(f"Scraped DBMF data for {latest_date}")
    print(f"New position rows scraped: {len(new_positions)}")
    print(f"Total position rows stored: {len(positions)}")
    print(f"Total description rows stored: {len(descriptions)}")
    print(f"Updated: {POSITIONS_PATH}")
    print(f"Updated: {DESCRIPTIONS_PATH}")


if __name__ == "__main__":
    main()
