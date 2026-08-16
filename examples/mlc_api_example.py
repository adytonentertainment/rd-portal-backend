"""
Example script demonstrating how to use the MLC API client
to retrieve registrations, authors, and publishers by ISRC
"""

import sys
import os
from pathlib import Path

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.libs.MLC import MLCClient
import json


def print_json(data, title=""):
    """Pretty print JSON data"""
    if title:
        print(f"\n{'='*60}")
        print(f"{title}")
        print('='*60)
    print(json.dumps(data, indent=2))


def example_search_by_isrc(client: MLCClient, isrc: str):
    """Example: Search for a recording by ISRC"""
    print(f"\n\n{'#'*60}")
    print(f"# Example 1: Search Recording by ISRC")
    print(f"{'#'*60}")

    try:
        result = client.search_recordings_by_isrc(isrc)
        print_json(result, f"Recording data for ISRC: {isrc}")

        # Extract MLC song code if available
        if result.get('recordings'):
            recording = result['recordings'][0] if isinstance(result['recordings'], list) else result
            mlc_song_code = recording.get('mlcSongCode')
            print(f"\nMLC Song Code: {mlc_song_code}")
            return mlc_song_code
    except Exception as e:
        print(f"Error: {str(e)}")
        return None


def example_get_complete_info(client: MLCClient, isrc: str):
    """Example: Get complete work info including writers and publishers"""
    print(f"\n\n{'#'*60}")
    print(f"# Example 2: Get Complete Work Info by ISRC")
    print(f"{'#'*60}")

    try:
        result = client.get_complete_work_info_by_isrc(isrc)

        if result.get('recording'):
            print_json(result['recording'], "Recording Information")

        if result.get('work'):
            work = result['work']

            print(f"\n\nWork Title: {work.get('primaryTitle')}")
            print(f"MLC Song Code: {work.get('mlcSongCode')}")
            print(f"ISWC: {work.get('iswc')}")

            # Print writers/authors
            if work.get('writers'):
                print(f"\n{'='*60}")
                print("AUTHORS/WRITERS:")
                print('='*60)
                for writer in work['writers']:
                    print(f"\nName: {writer.get('name')}")
                    print(f"IPI Number: {writer.get('ipiNumber')}")
                    print(f"Role Code: {writer.get('roleCode')}")
                    print(f"Chain ID: {writer.get('chainId')}")

            # Print publishers
            if work.get('publishers'):
                print(f"\n{'='*60}")
                print("PUBLISHERS:")
                print('='*60)
                for publisher in work['publishers']:
                    print(f"\nName: {publisher.get('name')}")
                    print(f"IPI Number: {publisher.get('ipiNumber')}")
                    print(f"Share Percentage: {publisher.get('sharePercentage')}%")
                    print(f"Chain ID: {publisher.get('chainId')}")

            # Print full work JSON
            print_json(work, "Complete Work Details (JSON)")

        if result.get('error'):
            print(f"\nWarning: {result['error']}")

    except Exception as e:
        print(f"Error: {str(e)}")


def example_search_by_title_artist(client: MLCClient, artist: str, title: str):
    """Example: Search recordings by artist and title"""
    print(f"\n\n{'#'*60}")
    print(f"# Example 3: Search by Artist and Title")
    print(f"{'#'*60}")

    try:
        result = client.search_recordings(artist=artist, title=title)
        print_json(result, f"Recordings for '{title}' by {artist}")
    except Exception as e:
        print(f"Error: {str(e)}")


def example_search_work_by_title_writer(client: MLCClient, title: str, writer_name: str):
    """Example: Search works by title and writer"""
    print(f"\n\n{'#'*60}")
    print(f"# Example 4: Search Works by Title and Writer")
    print(f"{'#'*60}")

    try:
        result = client.search_by_title_and_writer(title=title, writer_name=writer_name)
        print_json(result, f"Works for '{title}' by writer {writer_name}")
    except Exception as e:
        print(f"Error: {str(e)}")


def main():
    """Main example function"""
    print("="*60)
    print("MLC API Client Examples")
    print("="*60)

    # Initialize client (uses credentials from .env.development)
    try:
        client = MLCClient()
        print("✓ MLC Client initialized successfully")
    except Exception as e:
        print(f"✗ Failed to initialize MLC client: {str(e)}")
        print("\nMake sure you have set the following in your .env.development:")
        print("  - MLC_USERNAME")
        print("  - MLC_PASSWORD")
        print("  - MLC_API_URL")
        return

    # Example ISRC codes - replace with actual ISRCs you want to test
    example_isrc = "USRC17607839"  # Replace with a real ISRC
    example_artist = "The Beatles"
    example_title = "Hey Jude"
    example_writer = "Paul McCartney"

    # Run examples
    print("\n\nNote: Replace example ISRC codes with real ones from your catalog")

    # Example 1: Search by ISRC only
    mlc_song_code = example_search_by_isrc(client, example_isrc)

    # Example 2: Get complete work info (recording + writers + publishers)
    example_get_complete_info(client, example_isrc)

    # Example 3: Search by artist and title
    example_search_by_title_artist(client, example_artist, example_title)

    # Example 4: Search works by title and writer (requires authentication)
    example_search_work_by_title_writer(client, example_title, example_writer)

    print("\n\n" + "="*60)
    print("Examples completed!")
    print("="*60)


if __name__ == "__main__":
    # Load environment variables from .env.development
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / '.env.development'
    load_dotenv(env_path)

    main()
