"""
Fetch today's streaming data from Songstats API
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

# Track mapping (title -> songstats_track_id)
TRACK_SONGSTATS_IDS = {
    "Flowers": "4oidQMJbcuV",
    "Famous Hoes": "5KGl7eaZOJo",
    "On tha linë": "2Eo6iJ6sQZ4",
    "Flow 2000 - Remix": "5LEP0EkVk52",
    "Flow 2000": "6xewJMq08V4",
    "COMË N GO": "1LmY2eVdx2Q",
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


def update_database_with_today_data():
    """Fetch today's data and update database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get today's date
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Fetching Songstats data for: {today_str}")
    print(f"{'='*60}\n")

    updated_count = 0

    # Fetch data for each track
    for title, songstats_id in TRACK_SONGSTATS_IDS.items():
        try:
            # Get Spotify track ID from database
            cursor.execute(
                "SELECT spotify_track_id FROM Catalog WHERE title = ?", (title,)
            )
            result = cursor.fetchone()

            if not result:
                print(f"⚠️  Track '{title}' not found in database")
                continue

            spotify_track_id = result[0]

            # Check if we already have today's data
            cursor.execute(
                """
                SELECT COUNT(*) FROM PlayCountOverTime
                WHERE spotify_track_id = ?
                AND date(time_searched_at) = ?
            """,
                (spotify_track_id, today_str),
            )

            if cursor.fetchone()[0] > 0:
                print(f"✓ '{title}' already has today's data")
                continue

            # Fetch from Songstats
            print(f"⏳ Fetching '{title}' (ID: {songstats_id})...")
            data = fetch_songstats_historic_data(songstats_id, today_str, today_str)

            if not data or "items" not in data:
                print(f"✗ No data returned for '{title}'")
                continue

            # Extract data points
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

                # Insert into database
                cursor.execute(
                    """
                    INSERT INTO PlayCountOverTime
                    (spotify_track_id, playcount, youtube_playcount, time_searched_at)
                    VALUES (?, ?, ?, ?)
                """,
                    (spotify_track_id, spotify_streams, youtube_views, timestamp),
                )

                print(
                    f"  ✓ Added data for {date_str}: {spotify_streams:,} Spotify streams, {youtube_views:,} YouTube views"
                )
                updated_count += 1

            conn.commit()

        except Exception as e:
            print(f"✗ Error updating '{title}': {e}")
            conn.rollback()
            continue

    conn.close()

    print(f"\n{'='*60}")
    print(f"✅ Update complete: {updated_count} entries added")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    update_database_with_today_data()
