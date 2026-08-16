import csv
import io
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from app.database.session import get_session
from app.misc import get_spotify_access_token
from app.models.models import (
    RevenueStatement,
    RevenueTransaction,
    Songs,
    StatsCache,
    Subscription,
    SubscriptionTier,
    User,
    UserCatalog,
)
from app.routers.auth import get_user
from app.schemas import RoyaltyRequest, RoyaltyResponse
from app.services.notification_service import NotificationService
from app.settings.settings import get_settings
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

settings = get_settings()
royalty_router = APIRouter(tags=["Royalty"])

# Map 3-letter ISO codes to 2-letter so territories like USA/US merge
ALPHA3_TO_ALPHA2 = {
    "USA": "US", "CAN": "CA", "MEX": "MX", "GBR": "GB", "DEU": "DE",
    "FRA": "FR", "ITA": "IT", "ESP": "ES", "NLD": "NL", "SWE": "SE",
    "NOR": "NO", "DNK": "DK", "FIN": "FI", "POL": "PL", "BEL": "BE",
    "AUT": "AT", "CHE": "CH", "PRT": "PT", "IRL": "IE", "GRC": "GR",
    "JPN": "JP", "KOR": "KR", "CHN": "CN", "IND": "IN", "SGP": "SG",
    "THA": "TH", "MYS": "MY", "IDN": "ID", "PHL": "PH", "VNM": "VN",
    "TWN": "TW", "HKG": "HK", "AUS": "AU", "NZL": "NZ", "BRA": "BR",
    "ARG": "AR", "CHL": "CL", "COL": "CO", "PER": "PE", "ARE": "AE",
    "SAU": "SA", "ISR": "IL", "TUR": "TR", "ZAF": "ZA", "NGA": "NG",
    "EGY": "EG", "KEN": "KE", "RUS": "RU", "UKR": "UA", "CZE": "CZ",
    "HUN": "HU", "ROU": "RO",
}


def normalize_territory(code: str) -> str:
    """Normalize a territory code to 2-letter ISO form."""
    if not code:
        return code
    upper = code.upper()
    return ALPHA3_TO_ALPHA2.get(upper, upper)
spotify_access_token = get_spotify_access_token()


@royalty_router.get(
    "/royalty", status_code=status.HTTP_200_OK, response_model=RoyaltyResponse
)
async def get_royalty_per_stream(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    return RoyaltyResponse(royalty_per_stream=user.royalty_per_stream)


@royalty_router.patch(
    "/royalty", status_code=status.HTTP_200_OK, response_model=RoyaltyResponse
)
async def change_royalty_per_stream(
    royalty: RoyaltyRequest,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Inserts the playcount for the current day for all tracks in the users catalog."""

    user.royalty_per_stream = royalty.royalty_per_stream
    db.commit()
    db.refresh(user)
    return RoyaltyResponse(royalty_per_stream=user.royalty_per_stream)


def smart_detect_field(row: Dict[str, str], field_patterns: List[str]) -> str:
    """Smart field detection with fuzzy matching."""
    row_lower = {k.lower().strip(): v for k, v in row.items()}

    for pattern in field_patterns:
        # Exact match
        if pattern in row_lower:
            return row_lower[pattern]

        # Partial match
        for key, value in row_lower.items():
            if pattern in key or key in pattern:
                return value

    return ""


def normalize_header(header: str) -> str:
    """Normalize header for matching - remove special chars, lowercase."""
    import re

    return re.sub(r"[^a-z0-9\s]", "", header.lower().strip())


def detect_source_category(source: str) -> str:
    """Intelligently detect source category from platform name."""
    source_lower = source.lower()

    streaming_keywords = [
        "spotify",
        "apple music",
        "youtube",
        "tidal",
        "deezer",
        "amazon music",
        "pandora",
        "soundcloud",
        "digital/new media",
    ]
    performance_keywords = [
        "ascap",
        "bmi",
        "sesac",
        "prs",
        "socan",
        "apra",
        "gema",
        "sacem",
    ]
    mechanical_keywords = ["mechanical", "harry fox", "hfa", "cmrra"]
    sync_keywords = ["sync", "synchronization", "tv", "film", "movie", "commercial"]

    for keyword in streaming_keywords:
        if keyword in source_lower:
            return "streaming"

    for keyword in performance_keywords:
        if keyword in source_lower:
            return "performance"

    for keyword in mechanical_keywords:
        if keyword in source_lower:
            return "mechanical"

    for keyword in sync_keywords:
        if keyword in source_lower:
            return "sync"

    return "streaming"  # Default to streaming


def clean_currency_amount(value: str) -> float:
    """Extract numeric amount from string with currency symbols.

    Handles various formats:
    - Currency symbols: $, £, €, ¥, etc.
    - Thousands separators (both , and .)
    - European decimal format (comma as decimal separator)
    - Parentheses for negative numbers
    - K/M/B suffixes (1K = 1000, 1M = 1000000)
    """
    if not value:
        return 0.0

    cleaned = str(value).strip()

    # Remove currency symbols and whitespace
    cleaned = re.sub(r"[$£€¥₹₽¢\s]", "", cleaned)

    # Handle parentheses for negative numbers
    if "(" in cleaned and ")" in cleaned:
        cleaned = "-" + cleaned.replace("(", "").replace(")", "")

    # Handle K/M/B suffixes
    suffix_match = re.match(r"^([+-]?[\d.,]+)([kKmMbB])$", cleaned)
    if suffix_match:
        num_str = suffix_match.group(1)
        suffix = suffix_match.group(2).lower()
        multipliers = {"k": 1000, "m": 1000000, "b": 1000000000}
        try:
            # Handle European format in the number part
            if "," in num_str and "." not in num_str:
                num_str = num_str.replace(",", ".")
            elif "," in num_str and "." in num_str:
                num_str = num_str.replace(",", "")
            return abs(float(num_str) * multipliers.get(suffix, 1))
        except (ValueError, AttributeError):
            pass

    # Handle European decimal format (comma as decimal separator)
    # Pattern: "1.234,56" or "1234,56" (European) vs "1,234.56" (US)
    if "," in cleaned and "." in cleaned:
        # If comma comes after the last dot, it's European format
        last_comma = cleaned.rfind(",")
        last_dot = cleaned.rfind(".")
        if last_comma > last_dot:
            # European: 1.234,56 -> 1234.56
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # US: 1,234.56 -> 1234.56
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        # Could be European decimal (1234,56) or US thousands (1,234)
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            # Likely European decimal: 1234,56
            cleaned = cleaned.replace(",", ".")
        else:
            # Likely US thousands: 1,234,567
            cleaned = cleaned.replace(",", "")

    try:
        return abs(float(cleaned))
    except (ValueError, AttributeError):
        return 0.0


def parse_csv_line_with_quotes(line: str) -> List[str]:
    """Parse CSV line handling quoted fields properly."""
    result = []
    current = ""
    in_quotes = False

    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            result.append(current.strip())
            current = ""
        else:
            current += char

    result.append(current.strip())
    return result


def smart_parse_csv(content: bytes) -> List[Dict[str, Any]]:
    """Intelligent CSV parser that handles various royalty statement formats."""
    try:
        text = content.decode("utf-8")
        lines = text.split("\n")

        # Filter out empty lines
        lines = [line.strip() for line in lines if line.strip()]

        if len(lines) < 2:
            return []

        # Parse header and detect delimiter by counting occurrences
        header_line = lines[0]
        comma_count = header_line.count(",")
        semicolon_count = header_line.count(";")
        tab_count = header_line.count("\t")

        # Pick delimiter with most occurrences (semicolons common in European CSVs)
        if semicolon_count > comma_count and semicolon_count > tab_count:
            delimiter = ";"
        elif tab_count > comma_count:
            delimiter = "\t"
        else:
            delimiter = ","

        logger.info(
            f"CSV delimiter detection: comma={comma_count}, semicolon={semicolon_count}, tab={tab_count} -> using '{delimiter}'"
        )

        # Parse CSV
        csv_file = io.StringIO(text)
        reader = csv.DictReader(csv_file, delimiter=delimiter)

        transactions = []
        headers_logged = False

        for i, row in enumerate(reader):
            if not row:
                continue

            # Normalize keys
            normalized_row = {
                k.lower().strip() if k else f"col_{idx}": v.strip() if v else ""
                for idx, (k, v) in enumerate(row.items())
            }

            # Log headers once for debugging
            if not headers_logged:
                logger.info(f"CSV headers (normalized): {list(normalized_row.keys())}")
                headers_logged = True

            # Smart field extraction with comprehensive column name patterns
            # Aligned with iOS SmartCsvParser.swift for consistency

            date = smart_detect_field(
                normalized_row,
                [
                    "date",
                    "transaction_date",
                    "payment_date",
                    "period",
                    "month",
                    "sale_date",
                    "reporting_date",
                    "activity_date",
                    "transaction date",
                    "payment date",
                    "sale date",
                    "sale month",
                    "reporting period",
                    "period_end",
                    "report_date",
                    "statement_date",
                    "period date",
                    "settlement date",
                ],
            ) or datetime.now().strftime("%Y-%m-%d")

            # Source/PRO detection - comprehensive patterns
            source = (
                smart_detect_field(
                    normalized_row,
                    [
                        "source name",
                        "income source",
                        "source",
                        "pro",
                        "cmo",
                        "society",
                        "collecting society",
                        "collection society",
                        "performing rights",
                        "mechanical rights",
                        "revenue_source",
                        "partner",
                        "distributor",
                        "platform",
                        "service",
                        "store",
                        "dsp",
                        "streaming_service",
                        "sales_source",
                    ],
                )
                or "Unknown"
            )

            # Platform/DSP detection
            platform = (
                smart_detect_field(
                    normalized_row,
                    [
                        "platform",
                        "dsp",
                        "service",
                        "store",
                        "storefront",
                        "channel",
                        "digital service",
                        "streaming service",
                        "content_type",
                        "revenue type",
                        "sale type",
                        "transaction type",
                        "usage type",
                        "product type",
                    ],
                )
                or ""
            )

            # Product/Song detection - comprehensive patterns
            product = (
                smart_detect_field(
                    normalized_row,
                    [
                        "product",
                        "song",
                        "track",
                        "title",
                        "asset",
                        "work",
                        "content",
                        "release",
                        "album",
                        "single",
                        "track_name",
                        "song_title",
                        "track title",
                        "song title",
                        "composition",
                        "recording",
                        "asset name",
                        "content name",
                        "work title",
                    ],
                )
                or "Unknown"
            )

            # Territory detection - comprehensive patterns
            territory = (
                smart_detect_field(
                    normalized_row,
                    [
                        "royalty country code",
                        "territory",
                        "country",
                        "region",
                        "territory_code",
                        "country_code",
                        "location",
                        "market",
                        "country of sale",
                        "reporting territory",
                        "sale country",
                        "usage territory",
                        "reporting country",
                        "collection country",
                        "royalty country",
                    ],
                )
                or "US"
            )

            territory_name = (
                smart_detect_field(
                    normalized_row,
                    [
                        "territory_name",
                        "country_name",
                        "country name",
                        "full_country",
                        "territory name",
                    ],
                )
                or territory
            )

            currency = (
                smart_detect_field(
                    normalized_row,
                    ["currency", "currency_code", "curr", "ccy", "currency code"],
                )
                or "USD"
            )

            # Amount detection - COMPREHENSIVE patterns matching iOS parser
            # This is critical - many royalty statements use different column names
            amount_str = smart_detect_field(
                normalized_row,
                [
                    "amount",
                    "revenue",
                    "total",
                    "payment",
                    "earnings",
                    "net",
                    "gross",
                    "royalty",
                    "payout",
                    "income",
                    "total_amount",
                    "net_revenue",
                    "gross_revenue",
                    "total_revenue",
                    "payment_amount",
                    "royalty_amount",
                    "earned",
                    "net revenue",
                    "gross revenue",
                    "total earned",
                    "your revenue",
                    "partner share",
                    "net receipts",
                    "royalty net",
                    "label share",
                    "artist share",
                    "publisher share",
                    "writer share",
                    "mechanical",
                    "performance",
                    "sync fee",
                    "master recording",
                    "composition share",
                    "royalties",
                    "amount_paid",
                    "total payable",
                    "payable amount",
                    "amount due",
                    "due amount",
                    "statement amount",
                    "royalty payable",
                    "royalties payable",
                ],
            )

            amount = clean_currency_amount(amount_str)

            # Fallback: if no amount found, try to find any numeric value in the row
            if amount == 0 and not amount_str:
                for key, value in normalized_row.items():
                    if value and value.strip():
                        parsed_amount = clean_currency_amount(value)
                        if parsed_amount > 0:
                            # Avoid misinterpreting other numeric fields
                            key_lower = key.lower()
                            skip_keys = [
                                "quantity",
                                "qty",
                                "units",
                                "plays",
                                "streams",
                                "downloads",
                                "year",
                                "month",
                                "day",
                                "id",
                                "count",
                            ]
                            if not any(skip in key_lower for skip in skip_keys):
                                amount = parsed_amount
                                logger.info(
                                    f"Fallback: found amount {amount} in column '{key}'"
                                )
                                break

            # Artist detection - performing artist patterns only
            artist = (
                smart_detect_field(
                    normalized_row,
                    [
                        "artist",
                        "artist_name",
                        "performer",
                        "band",
                        "creator",
                        "artist name",
                        "performing artist",
                        "track artist",
                        "recording display artist name",
                        "featured artist",
                        "primary artist",
                    ],
                )
                or ""
            )

            # Writer/composer detection - separate from performing artist
            writer = smart_detect_field(
                normalized_row,
                [
                    "writer",
                    "composer",
                    "composers",
                    "songwriter",
                    "writer name",
                    "composer name",
                    "songwriter name",
                    "author",
                    "work writer list",
                    "participant name",
                    "urheber",
                    "auteur",
                ],
            )

            status = (
                smart_detect_field(
                    normalized_row, ["status", "payment_status", "state"]
                )
                or "paid"
            )

            # Category/Income type detection - comprehensive patterns
            category = smart_detect_field(
                normalized_row,
                [
                    "main income type name",
                    "income type name",
                    "category",
                    "type",
                    "income type",
                    "revenue category",
                    "royalty type",
                    "source category",
                    "revenue_type",
                    "income_type",
                    "usage category",
                    "usage_type",
                ],
            )

            if not category:
                category = detect_source_category(source)

            # Income period detection (important for royalty statements)
            income_period = smart_detect_field(
                normalized_row,
                [
                    "income period",
                    "royalty period",
                    "statement period",
                    "accounting period",
                    "revenue period",
                    "pay period",
                    "settlement period",
                    "period",
                    "reporting period",
                ],
            )

            # Fallback: if no single income period column, check for period start/end
            if not income_period:
                period_start = smart_detect_field(
                    normalized_row,
                    [
                        "period start",
                        "period_start",
                        "start period",
                        "start_period",
                        "period start date",
                        "start date",
                    ],
                )
                period_end = smart_detect_field(
                    normalized_row,
                    [
                        "period end",
                        "period_end",
                        "end period",
                        "end_period",
                        "period end date",
                        "end date",
                    ],
                )
                if period_start and period_end:
                    income_period = f"{period_start} - {period_end}"
                elif period_start:
                    income_period = period_start
                elif period_end:
                    income_period = period_end

            income_period_category = smart_detect_field(
                normalized_row,
                [
                    "income period category",
                    "period category",
                    "period type",
                    "income type",
                    "royalty type",
                    "statement type",
                    "revenue type",
                    "income category",
                ],
            )

            income_name = smart_detect_field(
                normalized_row,
                [
                    "original source (received)",
                    "original source",
                    "income name",
                    "revenue name",
                    "income description",
                    "revenue description",
                    "income title",
                    "revenue title",
                    "description",
                ],
            )

            # ISRC detection
            isrc = smart_detect_field(
                normalized_row,
                [
                    "isrc",
                    "isrc_code",
                    "isrccode",
                    "isrc code",
                    "international_standard_recording_code",
                ],
            )

            # Quantity/Streams detection
            quantity_str = smart_detect_field(
                normalized_row,
                [
                    "quantity",
                    "qty",
                    "units",
                    "plays",
                    "streams",
                    "downloads",
                    "unit_quantity",
                    "quantity_sold",
                    "stream_count",
                    "play_count",
                    "units sold",
                    "total plays",
                    "total streams",
                ],
            )

            # Only add if we have a valid amount
            if amount > 0:
                transaction = {
                    "id": f"uploaded-{datetime.now().timestamp()}-{i}",
                    "date": date,
                    "source": source,
                    "sourceCategory": category,
                    "platform": platform if platform else None,
                    "product": product,
                    "territory": normalize_territory(territory) if len(territory) <= 3 else territory[:2].upper(),
                    "territoryName": territory_name,
                    "currency": currency.upper() if currency else "USD",
                    "amount": amount,
                    "artist": artist if artist else None,
                    "writer": writer if writer else None,
                    "status": status.lower() if status else "paid",
                    "isrc": isrc if isrc else None,
                    "incomePeriod": income_period if income_period else None,
                    "incomePeriodCategory": income_period_category
                    if income_period_category
                    else None,
                    "incomeName": income_name if income_name else None,
                }
                transactions.append(transaction)

        logger.info(
            f"CSV parsing complete: {len(transactions)} valid transactions found"
        )
        if len(transactions) == 0:
            logger.warning("No valid transactions found. Common causes:")
            logger.warning("  - Amount column not detected (check column headers)")
            logger.warning("  - All amounts are 0 or negative")
            logger.warning("  - Amount values couldn't be parsed (check format)")
            # Log a sample row for debugging
            csv_file.seek(0)
            reader = csv.DictReader(csv_file, delimiter=delimiter)
            for i, row in enumerate(reader):
                if i == 0:
                    logger.warning(f"Sample row data: {row}")
                    # Try to find any amount-like values
                    for key, value in row.items():
                        if value and value.strip():
                            cleaned_amount = clean_currency_amount(value)
                            logger.warning(
                                f"  Column '{key}': '{value}' -> parsed as {cleaned_amount}"
                            )
                break

        return transactions
    except Exception as e:
        logger.error(f"Smart parsing error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Error parsing CSV: {str(e)}")


async def auto_sync_songs_to_catalog(
    transactions_data: List[Dict[str, Any]], user: User, db: Session, client_id: int = None
) -> Dict[str, Any]:
    """
    Automatically sync unique songs from uploaded statement to user's catalog.
    Searches Spotify for each song to get ISRC and spotify_track_id.
    """
    global spotify_access_token

    # Extract unique songs from transactions (by product/title + artist or writer)
    songs_map = {}
    for t in transactions_data:
        product = t.get("product", "").strip()
        artist = t.get("artist", "").strip()
        writer = t.get("writer", "").strip()
        isrc = t.get("isrc", "")

        if not product or product == "Unknown":
            continue

        # Use artist for key if available, otherwise use writer for deduplication
        key_name = artist if artist else writer
        key = f"{product.lower()}|{key_name.lower()}"
        if key not in songs_map:
            songs_map[key] = {
                "title": product,
                "artist": artist,
                "writer": writer,
                "isrc": isrc if isrc else "",
            }

    if not songs_map:
        return {
            "added": 0,
            "skipped": 0,
            "message": "No valid songs found in transactions",
        }

    # Pre-load user's existing catalog for fast matching
    # This helps resolve songs like "3AM" by writer "CALEB ZACKERY TOLIVER"
    # → "3AM by Loe Shimmy" already in catalog
    catalog_songs = (
        db.query(Songs)
        .join(UserCatalog)
        .filter(UserCatalog.user_id == user.id)
        .all()
    )
    # Build lookup indexes for catalog matching
    catalog_by_isrc = {}
    catalog_by_title = {}  # title -> list of Songs (multiple artists possible)
    for s in catalog_songs:
        if s.isrc:
            catalog_by_isrc[s.isrc.upper()] = s
        title_key = s.title.lower().strip() if s.title else ""
        if title_key:
            if title_key not in catalog_by_title:
                catalog_by_title[title_key] = []
            catalog_by_title[title_key].append(s)

    # Also build a set of catalog artist names for disambiguation
    catalog_artist_names = set()
    for s in catalog_songs:
        if s.artist:
            catalog_artist_names.add(s.artist.lower().strip())

    # Get Spotify access token if needed
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()

    added_count = 0
    skipped_items = []

    for song_data in songs_map.values():
        title = song_data["title"]
        artist = song_data["artist"]
        writer = song_data.get("writer", "")
        isrc = (
            song_data["isrc"]
            if song_data["isrc"] and song_data["isrc"] != "N/A"
            else None
        )
        spotify_track_id = None
        album = ""
        album_art = ""

        # Strategy 0: Check user's existing catalog by ISRC (instant match)
        if isrc and isrc.upper() in catalog_by_isrc:
            catalog_song = catalog_by_isrc[isrc.upper()]
            logger.info(
                f"Catalog ISRC match: '{title}' → '{catalog_song.title}' by {catalog_song.artist} (ISRC: {isrc})"
            )
            skipped_items.append(
                {"title": title, "artist": catalog_song.artist, "reason": "Already in catalog"}
            )
            continue

        # Strategy 0b: Check user's catalog by exact title match
        # (for publishing statements where ISRC may link to the same song)
        title_key = title.lower().strip()
        if not artist and title_key in catalog_by_title:
            catalog_matches = catalog_by_title[title_key]
            if len(catalog_matches) == 1:
                # Unambiguous: only one song with this title in catalog
                catalog_song = catalog_matches[0]
                logger.info(
                    f"Catalog title match: '{title}' → '{catalog_song.title}' by {catalog_song.artist}"
                )
                skipped_items.append(
                    {"title": title, "artist": catalog_song.artist, "reason": "Already in catalog"}
                )
                continue

        # Writer-to-artist resolution: if no performing artist but have writer,
        # resolve via MLC API before hitting Spotify
        if not artist and writer:
            try:
                from app.services.writer_resolution import resolve_artist_from_writer
                resolved = resolve_artist_from_writer(
                    title=title,
                    writer=writer,
                    isrc=isrc,
                    spotify_access_token=spotify_access_token,
                )
                if resolved and resolved.get("artist"):
                    artist = resolved["artist"]
                    if resolved.get("isrc") and not isrc:
                        isrc = resolved["isrc"]
                    logger.info(
                        f"Writer resolution: '{title}' writer '{writer}' → artist '{artist}' (via {resolved.get('source')})"
                    )
            except Exception as e:
                logger.warning(f"Writer resolution failed for '{title}' / '{writer}': {e}")

        try:
            search_url = "https://api.spotify.com/v1/search"
            headers = {"Authorization": f"Bearer {spotify_access_token}"}

            import re
            def _normalize(t):
                return re.sub(r'[^a-z0-9\s]', '', t.lower()).strip()

            def _artist_matches(sp_track, expected):
                exp = _normalize(expected)
                if not exp:
                    return True
                for a in sp_track.get("artists", []):
                    sp_a = _normalize(a.get("name", ""))
                    if exp in sp_a or sp_a in exp:
                        return True
                return False

            # Strategy 1: ISRC lookup with validation
            if isrc:
                params = {"q": f"isrc:{isrc}", "type": "track", "limit": 1}
                resp = requests.get(search_url, headers=headers, params=params)
                if resp.ok:
                    tracks = resp.json().get("tracks", {}).get("items", [])
                    if tracks:
                        spotify_track = tracks[0]
                        sp_title = _normalize(spotify_track.get("name", ""))
                        expected_title = _normalize(title)
                        # Validate ISRC result matches song title or artist
                        if sp_title == expected_title or expected_title in sp_title or sp_title in expected_title or _artist_matches(spotify_track, artist):
                            spotify_track_id = spotify_track.get("id", "")
                            album_info = spotify_track.get("album", {})
                            album = album_info.get("name", "")
                            images = album_info.get("images", [])
                            if images:
                                album_art = images[0].get("url", "")
                            sp_artists = spotify_track.get("artists", [])
                            if sp_artists:
                                artist = sp_artists[0].get("name", artist)
                            logger.info(f"Auto-sync matched by ISRC {isrc}: {title}")
                        else:
                            sp_name = spotify_track.get("name", "")
                            sp_art = ", ".join(a.get("name", "") for a in spotify_track.get("artists", []))
                            logger.warning(f"ISRC {isrc} returned wrong track: '{sp_name}' by {sp_art} (expected '{title}' by {artist}) — skipping")
                            isrc = None

            # Strategy 2: Title-based search with artist + catalog-aware disambiguation
            if not spotify_track_id:
                query_normalized = _normalize(title)
                params = {"q": f'track:"{title}" artist:"{artist}"', "type": "track", "limit": 10}
                resp = requests.get(search_url, headers=headers, params=params)
                if resp.ok:
                    tracks = resp.json().get("tracks", {}).get("items", [])
                    matched_track = None

                    # Collect all exact title matches
                    exact_matches = []
                    for t in tracks:
                        result_normalized = _normalize(t.get("name", ""))
                        if result_normalized == query_normalized:
                            exact_matches.append(t)

                    # Prefer matches where artist also matches
                    if len(exact_matches) == 1:
                        matched_track = exact_matches[0]
                    elif len(exact_matches) > 1:
                        # First: prefer artist match from search results
                        for t in exact_matches:
                            if _artist_matches(t, artist):
                                matched_track = t
                                break
                        # Second: prefer artist already in catalog
                        if not matched_track:
                            for t in exact_matches:
                                sp_artists = t.get("artists", [])
                                for sp_a in sp_artists:
                                    if sp_a.get("name", "").lower().strip() in catalog_artist_names:
                                        matched_track = t
                                        logger.info(
                                            f"Catalog-aware disambiguation: '{title}' → {sp_a.get('name')} (in catalog)"
                                        )
                                        break
                                if matched_track:
                                    break
                        # Fall back to first exact match
                        if not matched_track:
                            matched_track = exact_matches[0]

                    # If no results with artist filter, retry without
                    if not matched_track and not exact_matches:
                        params2 = {"q": f'track:"{title}"', "type": "track", "limit": 10}
                        resp2 = requests.get(search_url, headers=headers, params=params2)
                        if resp2.ok:
                            for t in resp2.json().get("tracks", {}).get("items", []):
                                if _normalize(t.get("name", "")) == query_normalized:
                                    if _artist_matches(t, artist):
                                        matched_track = t
                                        break
                            # Last resort: title match only
                            if not matched_track:
                                for t in resp2.json().get("tracks", {}).get("items", []):
                                    if _normalize(t.get("name", "")) == query_normalized:
                                        matched_track = t
                                        break

                    if matched_track:
                        spotify_track_id = matched_track.get("id", "")
                        album_info = matched_track.get("album", {})
                        album = album_info.get("name", "")
                        images = album_info.get("images", [])
                        if images:
                            album_art = images[0].get("url", "")
                        sp_artists = matched_track.get("artists", [])
                        if sp_artists:
                            artist = sp_artists[0].get("name", artist)
                        # Use ISRC from Spotify result if we didn't have one
                        ext_ids = matched_track.get("external_ids", {})
                        if not isrc and ext_ids.get("isrc"):
                            isrc = ext_ids["isrc"]
                        logger.info(f"Auto-sync matched by title '{title}': {artist}")
                    else:
                        skipped_items.append(
                            {"title": title, "artist": artist, "reason": "No exact title match on Spotify"}
                        )
                        continue
                else:
                    skipped_items.append(
                        {"title": title, "artist": artist, "reason": "Spotify search failed"}
                    )
                    continue

        except Exception as e:
            logger.error(f"Error searching Spotify for {title}: {e}")
            skipped_items.append(
                {"title": title, "artist": artist, "reason": f"Search error: {str(e)}"}
            )
            continue

        # Skip if no spotify_track_id found
        if not spotify_track_id:
            skipped_items.append(
                {
                    "title": title,
                    "artist": artist,
                    "reason": "Could not find on Spotify",
                }
            )
            continue

        # Check if song exists in global Songs table
        existing_song = (
            db.query(Songs).filter(Songs.spotify_track_id == spotify_track_id).first()
        )

        if not existing_song and isrc:
            existing_song = db.query(Songs).filter(Songs.isrc == isrc).first()

        if existing_song:
            song_id = existing_song.id
        else:
            # Create new song - use Spotify data for artist/title
            new_song = Songs(
                spotify_track_id=spotify_track_id,
                isrc=isrc,
                title=title,
                artist=artist,
                album=album,
                album_art=album_art,
                created_at=datetime.now(),
            )
            try:
                db.add(new_song)
                db.commit()
                db.refresh(new_song)
                song_id = new_song.id
            except IntegrityError:
                db.rollback()
                filters = [Songs.spotify_track_id == spotify_track_id]
                if isrc:
                    filters.append(Songs.isrc == isrc)
                from sqlalchemy import or_
                existing_song = (
                    db.query(Songs)
                    .filter(or_(*filters))
                    .first()
                )
                if existing_song:
                    song_id = existing_song.id
                else:
                    skipped_items.append(
                        {"title": title, "artist": artist, "reason": "Database error"}
                    )
                    continue

        # Check if user already has this song in their catalog
        existing_user_catalog = (
            db.query(UserCatalog)
            .filter(UserCatalog.user_id == user.id, UserCatalog.song_id == song_id)
            .first()
        )

        if existing_user_catalog:
            skipped_items.append(
                {"title": title, "artist": artist, "reason": "Already in catalog"}
            )
            continue

        # Add to user's catalog
        user_catalog_entry = UserCatalog(
            user_id=user.id,
            song_id=song_id,
            client_id=client_id,
            date_added=datetime.now(),
        )

        db.add(user_catalog_entry)
        db.commit()

        added_count += 1
        logger.info(f"Auto-synced to catalog: {title} by {artist}")

    return {
        "added": added_count,
        "skipped": len(skipped_items),
        "skipped_items": skipped_items,
        "message": f"Auto-synced {added_count} songs to catalog. {len(skipped_items)} skipped.",
    }


@royalty_router.post("/revenue/upload", status_code=status.HTTP_200_OK)
async def upload_royalty_statement(
    file: UploadFile = File(...),
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Upload and parse royalty statement files (CSV, Excel, PDF)."""

    # Validate file type
    allowed_extensions = [".csv", ".xlsx", ".xls", ".pdf"]
    file_extension = (
        file.filename.lower().split(".")[-1] if "." in file.filename else ""
    )

    if f".{file_extension}" not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed types: {', '.join(allowed_extensions)}",
        )

    # Read file content
    content = await file.read()

    # Parse based on file type
    transactions = []

    if file_extension == "csv":
        transactions = smart_parse_csv(content)
    elif file_extension in ["xlsx", "xls"]:
        # For Excel files, you would need openpyxl or xlrd library
        raise HTTPException(
            status_code=501,
            detail="Excel file parsing not yet implemented. Please use CSV format.",
        )
    elif file_extension == "pdf":
        # For PDF files, you would need PyPDF2 or pdfplumber library
        raise HTTPException(
            status_code=501,
            detail="PDF file parsing not yet implemented. Please use CSV format.",
        )

    if not transactions:
        raise HTTPException(
            status_code=400, detail="No valid transactions found in the file"
        )

    return {
        "message": "File processed successfully",
        "filename": file.filename,
        "transactionCount": len(transactions),
        "transactions": transactions,
    }


@royalty_router.post("/revenue/transactions/bulk", status_code=status.HTTP_200_OK)
async def save_transactions_bulk(
    data: Dict[str, Any],
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Save transactions in bulk from an uploaded statement."""

    transactions_data = data.get("transactions", [])
    filename = data.get("filename", "Unknown")
    client_id = data.get("clientId")  # Optional client ID for multi-client support
    profile_id = data.get("profileId")  # Optional profile ID for auto-detect tracking

    if not transactions_data:
        raise HTTPException(status_code=400, detail="No transactions provided")

    # client_id is optional - transactions without a client are visible in "All Clients" view

    # Calculate totals
    total_amount = sum(t.get("amount", 0) for t in transactions_data)
    transaction_count = len(transactions_data)

    # Get statement date from first transaction if available
    statement_date = None
    if transactions_data and "date" in transactions_data[0]:
        try:
            statement_date = datetime.fromisoformat(
                transactions_data[0]["date"].replace("Z", "+00:00")
            )
        except:
            statement_date = datetime.now()

    # Create revenue statement
    statement = RevenueStatement(
        user_id=user.id,
        client_id=client_id,
        filename=filename,
        upload_date=datetime.now(),
        statement_date=statement_date,
        transaction_count=transaction_count,
        total_amount=total_amount,
        file_size=data.get("fileSize", "Unknown"),
        profile_id=profile_id,
    )

    db.add(statement)
    db.flush()  # Get the statement ID

    # Create transactions
    for t_data in transactions_data:
        transaction = RevenueTransaction(
            statement_id=statement.id,
            user_id=user.id,
            date=t_data.get("date", datetime.now().isoformat()),
            source=t_data.get("source", "Unknown"),
            source_category=t_data.get("sourceCategory"),
            platform=t_data.get("platform"),
            product=t_data.get("product"),
            territory=t_data.get("territory"),
            territory_name=t_data.get("territoryName"),
            currency=t_data.get("currency", "USD"),
            amount=t_data.get("amount", 0),
            artist=t_data.get("artist"),
            writer=t_data.get("writer"),
            status=t_data.get("status", "paid"),
            category=t_data.get("category"),
            income_name=t_data.get("incomeName"),
            income_period=t_data.get("incomePeriod"),
            income_period_category=t_data.get("incomePeriodCategory"),
            isrc=t_data.get("isrc"),
        )
        db.add(transaction)

    db.commit()

    # AUTO-SYNC: Extract unique songs and add to catalog
    sync_results = await auto_sync_songs_to_catalog(
        transactions_data=transactions_data, user=user, db=db, client_id=client_id
    )

    # Create notification for statement processed
    try:
        notification_service = NotificationService(db)
        notification_service.create_statement_processed_notification(
            user_id=user.id,
            statement_id=statement.id,
            filename=filename,
            transaction_count=transaction_count,
            total_amount=total_amount,
            songs_synced=sync_results.get("added", 0),
        )
    except Exception as e:
        logger.warning(f"Failed to create statement notification: {e}")

    return {
        "message": "Transactions saved successfully",
        "statementId": statement.id,
        "transactionCount": transaction_count,
        "totalAmount": total_amount,
        "catalogSync": sync_results,
    }


@royalty_router.get("/revenue/statements", status_code=status.HTTP_200_OK)
async def get_user_statements(
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
    client_id: int = None,
):
    """Get all revenue statements for the current user."""

    query = db.query(RevenueStatement).filter(RevenueStatement.user_id == user.id)
    if client_id is not None:
        query = query.filter(RevenueStatement.client_id == client_id)
    statements = query.order_by(RevenueStatement.upload_date.desc()).all()

    return {
        "statements": [
            {
                "id": s.id,
                "filename": s.filename,
                "uploadDate": s.upload_date.isoformat(),
                "statementDate": s.statement_date.isoformat()
                if s.statement_date
                else None,
                "transactionCount": s.transaction_count,
                "totalAmount": s.total_amount,
                "fileSize": s.file_size,
                "clientId": s.client_id,
            }
            for s in statements
        ]
    }


@royalty_router.get("/revenue/transactions", status_code=status.HTTP_200_OK)
async def get_user_transactions(
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
    client_id: int = None,
):
    """Get all revenue transactions for the current user, optionally filtered by client."""

    query = db.query(RevenueTransaction).filter(RevenueTransaction.user_id == user.id)

    # Filter by client_id through the statement relationship
    if client_id is not None:
        query = query.join(RevenueStatement).filter(
            RevenueStatement.client_id == client_id
        )

    transactions = query.order_by(RevenueTransaction.date.desc()).all()

    return {
        "transactions": [
            {
                "id": f"db-{t.id}",
                "statementId": t.statement_id,
                "date": t.date,
                "source": t.source,
                "sourceCategory": t.source_category,
                "platform": t.platform,
                "product": t.product,
                "territory": t.territory,
                "territoryName": t.territory_name,
                "currency": t.currency,
                "amount": t.amount,
                "artist": t.artist,
                "status": t.status,
                "category": t.category,
                "incomeName": t.income_name,
                "incomePeriod": t.income_period,
                "incomePeriodCategory": t.income_period_category,
                "isrc": t.isrc,
            }
            for t in transactions
        ]
    }


@royalty_router.delete(
    "/revenue/statements/{statement_id}", status_code=status.HTTP_200_OK
)
async def delete_statement(
    statement_id: int,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Delete a revenue statement and all its transactions."""

    statement = (
        db.query(RevenueStatement)
        .filter(
            RevenueStatement.id == statement_id, RevenueStatement.user_id == user.id
        )
        .first()
    )

    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")

    db.delete(statement)  # Cascade will delete transactions
    db.commit()

    return {"message": "Statement deleted successfully"}


@royalty_router.put(
    "/revenue/statements/{statement_id}/update-mappings", status_code=status.HTTP_200_OK
)
async def update_statement_mappings(
    statement_id: int,
    mapping_data: dict,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Update source/platform mappings for all transactions in a statement."""

    statement = (
        db.query(RevenueStatement)
        .filter(
            RevenueStatement.id == statement_id, RevenueStatement.user_id == user.id
        )
        .first()
    )

    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")

    # Update all transactions for this statement
    transactions = (
        db.query(RevenueTransaction)
        .filter(RevenueTransaction.statement_id == statement_id)
        .all()
    )

    source = mapping_data.get("source")
    platform = mapping_data.get("platform")

    updated_count = 0
    for t in transactions:
        if source:
            t.source = source
        if platform:
            t.platform = platform
        updated_count += 1

    db.commit()

    return {
        "message": f"Updated {updated_count} transactions",
        "updated_count": updated_count,
    }


@royalty_router.post("/revenue/analyze-for-catalog", status_code=status.HTTP_200_OK)
async def analyze_revenue_for_catalog(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    """
    Analyze revenue transactions and find songs that can be added to catalog.
    Returns songs found by ISRC and songs not found (need manual addition).
    """

    # Get all transactions with ISRC for this user
    transactions_with_isrc = (
        db.query(RevenueTransaction)
        .filter(
            RevenueTransaction.user_id == user.id,
            RevenueTransaction.isrc.isnot(None),
            RevenueTransaction.isrc != "",
        )
        .all()
    )

    if not transactions_with_isrc:
        return {
            "message": "No transactions with ISRC found",
            "found": [],
            "notFound": [],
            "totalRevenue": 0,
        }

    # Get unique ISRCs and group transactions
    isrc_groups = {}
    for transaction in transactions_with_isrc:
        isrc = transaction.isrc.strip().upper()
        if isrc not in isrc_groups:
            isrc_groups[isrc] = {
                "isrc": isrc,
                "product": transaction.product,
                "artist": transaction.artist,
                "totalRevenue": 0,
                "transactionCount": 0,
                "transactions": [],
            }
        isrc_groups[isrc]["totalRevenue"] += transaction.amount
        isrc_groups[isrc]["transactionCount"] += 1
        isrc_groups[isrc]["transactions"].append(
            {
                "id": transaction.id,
                "date": transaction.date,
                "source": transaction.source,
                "amount": transaction.amount,
                "territory": transaction.territory,
            }
        )

    # Check which ISRCs are already in catalog
    existing_catalog = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.isrc.in_(list(isrc_groups.keys())),
        )
        .all()
    )

    existing_isrcs = {c.isrc.strip().upper() for c in existing_catalog if c.isrc}

    # Separate found vs not found
    found = []
    not_found = []
    total_revenue = 0

    for isrc, data in isrc_groups.items():
        total_revenue += data["totalRevenue"]

        song_data = {
            "isrc": data["isrc"],
            "songTitle": data["product"],
            "artist": data["artist"],
            "totalRevenue": data["totalRevenue"],
            "transactionCount": data["transactionCount"],
            "transactions": data["transactions"],
        }

        if isrc in existing_isrcs:
            # Find the catalog entry
            catalog_entry = next(
                c for c in existing_catalog if c.isrc and c.isrc.strip().upper() == isrc
            )
            song_data["catalogId"] = catalog_entry.id
            song_data["inCatalog"] = True
            found.append(song_data)
        else:
            song_data["inCatalog"] = False
            not_found.append(song_data)

    return {
        "message": f"Found {len(found)} songs in catalog, {len(not_found)} songs not in catalog",
        "found": found,
        "notFound": not_found,
        "totalRevenue": total_revenue,
        "totalSongs": len(isrc_groups),
    }


@royalty_router.post("/revenue/expected-vs-actual", status_code=status.HTTP_200_OK)
async def calculate_expected_vs_actual_revenue(
    data: Dict[str, Any],
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Calculate expected publishing revenue based on streaming data vs actual reported revenue.
    - If songTitle is provided: returns expected vs actual for that specific song
    - If songTitle is null/empty: returns aggregated expected vs actual for ALL songs in catalog
    """

    song_title = data.get("songTitle")
    if song_title:
        song_title = song_title.strip()

    # Publishing rate per stream (industry standard ~$0.0009 per stream)
    PUBLISHING_RATE_PER_STREAM = 0.0009

    # Case 1: Calculate for ALL songs in catalog (when no song title provided)
    if not song_title:
        # Get all catalog entries for this user
        catalog_entries = (
            db.query(UserCatalog)
            .join(Songs)
            .filter(UserCatalog.user_id == user.id)
            .all()
        )

        # Aggregate expected revenue from all songs by date
        expected_revenue_by_date = {}  # {date: total_expected_revenue}

        for catalog_entry in catalog_entries:
            song = catalog_entry.song
            publishing_split = catalog_entry.publishing_royalty or 0

            if not song.spotify_track_id:
                continue

            # Get streaming stats for this song
            stats_data = (
                db.query(StatsCache)
                .filter(StatsCache.spotify_track_id == song.spotify_track_id)
                .order_by(StatsCache.date_added.asc())
                .all()
            )

            for stat in stats_data:
                total_streams = (stat.spotify_playcount or 0) + (
                    stat.youtube_playcount or 0
                )
                expected_revenue = (
                    total_streams * PUBLISHING_RATE_PER_STREAM * publishing_split
                )

                date_key = stat.date_added.isoformat() if stat.date_added else None
                if date_key:
                    if date_key not in expected_revenue_by_date:
                        expected_revenue_by_date[date_key] = 0
                    expected_revenue_by_date[date_key] += expected_revenue

        # Convert to list format
        expected_revenue_points = [
            {"date": date_str, "expectedRevenue": round(revenue, 2)}
            for date_str, revenue in sorted(expected_revenue_by_date.items())
        ]

        # Get ALL actual revenue transactions
        actual_transactions = (
            db.query(RevenueTransaction)
            .filter(RevenueTransaction.user_id == user.id)
            .order_by(RevenueTransaction.date.asc())
            .all()
        )

        actual_revenue_points = []
        for transaction in actual_transactions:
            actual_revenue_points.append(
                {
                    "date": transaction.date,
                    "amount": transaction.amount,
                    "source": transaction.source,
                    "incomeName": transaction.income_name,
                }
            )

        # Calculate total expected (use most recent cumulative)
        total_expected = (
            expected_revenue_points[-1]["expectedRevenue"]
            if expected_revenue_points
            else 0
        )

        return {
            "message": "Revenue comparison calculated for ALL songs",
            "songTitle": "All Songs",
            "artist": "All Artists",
            "publishingSplit": None,
            "expectedRevenue": expected_revenue_points,
            "actualRevenue": actual_revenue_points,
            "totalExpected": total_expected,
            "totalActual": sum(p["amount"] for p in actual_revenue_points),
        }

    # Case 2: Calculate for specific song (original logic)
    # Find the song in catalog by title
    # Try multiple search variations to handle special characters (e.g., "jus better" should match "Jus bëtter")
    catalog_entry = (
        db.query(UserCatalog)
        .join(Songs)
        .filter(UserCatalog.user_id == user.id, Songs.title.ilike(f"%{song_title}%"))
        .first()
    )

    # If not found, try with each word separately
    if not catalog_entry and " " in song_title:
        words = song_title.split()
        for word in words:
            if len(word) >= 3:  # Only search for words with 3+ characters
                catalog_entry = (
                    db.query(UserCatalog)
                    .join(Songs)
                    .filter(
                        UserCatalog.user_id == user.id, Songs.title.ilike(f"%{word}%")
                    )
                    .first()
                )
                if catalog_entry:
                    break

    if not catalog_entry:
        return {
            "message": "Song not found in catalog",
            "expectedRevenue": [],
            "actualRevenue": [],
            "songTitle": song_title,
        }

    song = catalog_entry.song
    publishing_split = catalog_entry.publishing_royalty or 0

    # Get historical streaming data from StatsCache
    stats_data = (
        db.query(StatsCache)
        .filter(StatsCache.spotify_track_id == song.spotify_track_id)
        .order_by(StatsCache.date_added.asc())
        .all()
    )

    # Calculate expected revenue from streams
    expected_revenue_points = []
    for stat in stats_data:
        # Calculate streams for this period (spotify + youtube)
        total_streams = (stat.spotify_playcount or 0) + (stat.youtube_playcount or 0)

        # Calculate expected publishing revenue
        expected_revenue = total_streams * PUBLISHING_RATE_PER_STREAM * publishing_split

        expected_revenue_points.append(
            {
                "date": stat.date_added.isoformat() if stat.date_added else None,
                "streams": total_streams,
                "expectedRevenue": round(expected_revenue, 2),
            }
        )

    # Get actual revenue transactions for this song
    actual_transactions = (
        db.query(RevenueTransaction)
        .filter(
            RevenueTransaction.user_id == user.id,
            RevenueTransaction.product.ilike(f"%{song_title}%"),
        )
        .order_by(RevenueTransaction.date.asc())
        .all()
    )

    actual_revenue_points = []
    for transaction in actual_transactions:
        actual_revenue_points.append(
            {
                "date": transaction.date,
                "amount": transaction.amount,
                "source": transaction.source,
                "incomeName": transaction.income_name,
            }
        )

    # Calculate total expected revenue using ONLY the most recent stream count
    # (since StatsCache contains cumulative totals, not daily increments)
    total_expected = 0
    if expected_revenue_points:
        # Get the most recent (last) data point which has the highest cumulative stream count
        most_recent = expected_revenue_points[-1]
        total_expected = most_recent["expectedRevenue"]

    return {
        "message": "Revenue comparison calculated successfully",
        "songTitle": song.title,
        "artist": song.artist,
        "publishingSplit": publishing_split,
        "expectedRevenue": expected_revenue_points,
        "actualRevenue": actual_revenue_points,
        "totalExpected": total_expected,
        "totalActual": sum(p["amount"] for p in actual_revenue_points),
    }


def normalize_title(title: str) -> str:
    """
    Normalize song title for fuzzy matching by removing special characters,
    accents, and converting to lowercase.
    """
    import unicodedata

    # Normalize unicode characters (ë -> e, etc.)
    normalized = unicodedata.normalize("NFKD", title)
    # Remove accents/diacritics
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    # Convert to lowercase and strip
    normalized = normalized.lower().strip()
    # Remove special characters but keep spaces and alphanumeric
    normalized = "".join(c if c.isalnum() or c.isspace() else " " for c in normalized)
    # Collapse multiple spaces
    normalized = " ".join(normalized.split())
    return normalized


@royalty_router.get("/revenue/missing-from-statements", status_code=status.HTTP_200_OK)
async def get_songs_missing_from_statements(
    client_id: Optional[int] = None,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Returns list of songs in user's catalog that don't have any revenue statement data.
    Useful for identifying which songs might be missing from royalty statements.
    Uses fuzzy matching to handle spelling variations and special characters.
    """

    # Get all songs in user's catalog, optionally filtered by client
    catalog_query = (
        db.query(UserCatalog).join(Songs).filter(UserCatalog.user_id == user.id)
    )
    if client_id is not None:
        catalog_query = catalog_query.filter(UserCatalog.client_id == client_id)
    catalog_songs = catalog_query.all()

    # Get all unique song titles from revenue transactions, optionally filtered by client
    transactions_query = (
        db.query(RevenueTransaction)
        .join(RevenueStatement, RevenueTransaction.statement_id == RevenueStatement.id)
        .filter(RevenueTransaction.user_id == user.id)
    )
    if client_id is not None:
        transactions_query = transactions_query.filter(
            RevenueStatement.client_id == client_id
        )
    transactions = transactions_query.all()

    # Create a set of normalized song titles that appear in statements
    songs_in_statements = set()
    for t in transactions:
        if t.product:
            normalized = normalize_title(t.product)
            if normalized:  # Only add non-empty normalized titles
                songs_in_statements.add(normalized)

    # Find catalog songs not in statements
    missing_songs = []
    for catalog_entry in catalog_songs:
        song = catalog_entry.song
        normalized_catalog_title = normalize_title(song.title)

        # Check if normalized song title appears in any transaction
        if normalized_catalog_title not in songs_in_statements:
            # Get streaming stats to show potential revenue
            stats = (
                db.query(StatsCache)
                .filter(StatsCache.spotify_track_id == song.spotify_track_id)
                .order_by(StatsCache.date_added.desc())
                .first()
            )

            total_streams = 0
            if stats:
                total_streams = (stats.spotify_playcount or 0) + (
                    stats.youtube_playcount or 0
                )

            # Calculate potential expected revenue based on streams
            publishing_split = catalog_entry.publishing_royalty or 0
            PUBLISHING_RATE_PER_STREAM = 0.0009
            potential_revenue = (
                total_streams * PUBLISHING_RATE_PER_STREAM * publishing_split
            )

            missing_songs.append(
                {
                    "catalogId": catalog_entry.id,
                    "songId": song.id,
                    "title": song.title,
                    "artist": song.artist,
                    "isrc": song.isrc,
                    "spotifyTrackId": song.spotify_track_id,
                    "totalStreams": total_streams,
                    "publishingSplit": publishing_split,
                    "potentialRevenue": round(potential_revenue, 2),
                    "albumArt": song.album_art,
                }
            )

    # Sort by potential revenue (highest first)
    missing_songs.sort(key=lambda x: x["potentialRevenue"], reverse=True)

    return {
        "message": f"Found {len(missing_songs)} songs in catalog without statement data",
        "totalCatalogSongs": len(catalog_songs),
        "songsWithStatements": len(catalog_songs) - len(missing_songs),
        "missingFromStatements": len(missing_songs),
        "songs": missing_songs,
    }
