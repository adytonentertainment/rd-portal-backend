"""
Daily full database update from Songstats API
Fetches all historical data from release date to ensure accuracy
"""

import sqlite3
import requests
import os
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Songstats API configuration
SONGSTATS_API_KEY = os.getenv("SONGSTATS_API_KEY")
SONGSTATS_API_BASE = "https://api.songstats.com/enterprise/v1"

# Database - Get from environment variable
DB_PATH = os.getenv("SQLALCHEMY_DATABASE_URL", "sqlite:///./tunescan_development.db").replace("sqlite:///./", "")

# Track mapping (title -> songstats_track_id, release_date)
TRACK_CONFIG = {
    "Flowers": {"songstats_id": "4oidQMJbcuV", "release_date": "2023-01-13"},
    "Famous Hoes": {"songstats_id": "5KGl7eaZOJo", "release_date": "2019-07-19"},
    "On tha linë": {"songstats_id": "2Eo6iJ6sQZ4", "release_date": "2022-02-18"},
    "Flow 2000 - Remix": {"songstats_id": "5LEP0EkVk52", "release_date": "2022-03-18"},
    "Flow 2000": {"songstats_id": "6xewJMq08V4", "release_date": "2021-10-15"},
    "COMË N GO": {"songstats_id": "1LmY2eVdx2Q", "release_date": "2025-08-01"},
}


def fetch_songstats_historic_data(songstats_track_id, start_date, end_date):
    """Fetch historic stats for a track"""
    url = f"{SONGSTATS_API_BASE}/tracks/historic_stats"

    headers = {"apikey": SONGSTATS_API_KEY, "Accept": "application/json"}

    params = {
        "songstats_track_id": songstats_track_id,
        "start_date": start_date,
        "end_date": end_date,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return None


def update_full_database():
    """Fetch full historical data and update database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get today's date
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    print(f"\n{'='*80}")
    print(f"DAILY FULL DATABASE UPDATE - {today_str}")
    print(f"Fetching complete history from release dates to today")
    print(f"{'='*80}\n")

    total_updated = 0
    total_new = 0

    # Process each track
    for title, config in TRACK_CONFIG.items():
        try:
            songstats_id = config["songstats_id"]
            release_date = config["release_date"]

            # Get Spotify track ID from database
            cursor.execute(
                "SELECT spotify_track_id FROM Catalog WHERE title = ?", (title,)
            )
            result = cursor.fetchone()

            if not result:
                print(f"⚠️  Track '{title}' not found in database")
                continue

            spotify_track_id = result[0]

            print(f"\n📀 {title}")
            print(f"   Release: {release_date} | Songstats ID: {songstats_id}")

            # Fetch full history from release date to today
            print(f"   ⏳ Fetching complete history...")
            data = fetch_songstats_historic_data(songstats_id, release_date, today_str)

            if not data or "items" not in data:
                print(f"   ✗ No data returned")
                continue

            new_entries = 0
            updated_entries = 0

            # Process each data point
            for item in data["items"]:
                date_str = item.get("date")

                # Find Spotify and YouTube data
                spotify_entry = next(
                    (
                        s
                        for s in item.get("sources", [])
                        if s.get("source") == "spotify"
                    ),
                    None,
                )
                youtube_entry = next(
                    (
                        s
                        for s in item.get("sources", [])
                        if s.get("source") == "youtube"
                    ),
                    None,
                )

                spotify_streams = (
                    spotify_entry.get("streams_total", 0) if spotify_entry else 0
                )
                youtube_views = (
                    youtube_entry.get("video_views_total", 0) if youtube_entry else 0
                )

                # Create timestamp
                timestamp = datetime.strptime(date_str, "%Y-%m-%d")

                # Check if entry exists for this date
                cursor.execute(
                    """
                    SELECT id, playcount, youtube_playcount FROM PlayCountOverTime
                    WHERE spotify_track_id = ?
                    AND date(time_searched_at) = ?
                """,
                    (spotify_track_id, date_str),
                )

                existing = cursor.fetchone()

                if existing:
                    # Update existing entry if data changed
                    existing_id, existing_spotify, existing_youtube = existing
                    if (
                        existing_spotify != spotify_streams
                        or existing_youtube != youtube_views
                    ):
                        cursor.execute(
                            """
                            UPDATE PlayCountOverTime
                            SET playcount = ?, youtube_playcount = ?
                            WHERE id = ?
                        """,
                            (spotify_streams, youtube_views, existing_id),
                        )
                        updated_entries += 1
                else:
                    # Insert new entry
                    cursor.execute(
                        """
                        INSERT INTO PlayCountOverTime
                        (spotify_track_id, playcount, youtube_playcount, time_searched_at)
                        VALUES (?, ?, ?, ?)
                    """,
                        (spotify_track_id, spotify_streams, youtube_views, timestamp),
                    )
                    new_entries += 1

            conn.commit()

            print(
                f"   ✓ Processed: {new_entries} new entries, {updated_entries} updated"
            )
            total_new += new_entries
            total_updated += updated_entries

        except Exception as e:
            print(f"   ✗ Error: {e}")
            conn.rollback()
            continue

    conn.close()

    print(f"\n{'='*80}")
    print(f"✅ FULL UPDATE COMPLETE")
    print(f"   New entries: {total_new}")
    print(f"   Updated entries: {total_updated}")
    print(f"   Total changes: {total_new + total_updated}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    update_full_database()
