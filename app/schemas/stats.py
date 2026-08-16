from pydantic import BaseModel
from datetime import datetime, date
from typing import List, Optional, Dict, Any


class Playcount(BaseModel):
    source: Optional[str] = None
    date: datetime
    playcount: int

    class Config:
        # Exclude fields that are None from the JSON response
        exclude_none = True

class PublishingRoyalty(BaseModel):
    date: datetime
    publishing_royalty: float

class MasterRoyalty(BaseModel):
    date: datetime
    master_royalty: float

# playcount of a single song, this is close to what songstats returns
# this is what should be cached

class PlayCountHistoryTrack(BaseModel):
    title: Optional[str] = None
    release_date: Optional[datetime] = None
    artists: Optional[List[str]] = None

    # different ways to identify a song
    spotify_track_id: Optional[str] = None
    apple_music_track_id: Optional[str] = None
    youtube_track_id: Optional[str] = None
    deezer_track_id: Optional[str] = None

    spotify: Optional[List[Playcount]] = []
    apple_music: Optional[List[Playcount]] = []
    youtube: Optional[List[Playcount]] = []
    deezer: Optional[List[Playcount]] = []

    class Config:
        exclude_none = True

# this is what our API should return.

class PlayCountHistoryAggregated(BaseModel):
    spotify: Optional[List[Playcount]] = []
    apple_music: Optional[List[Playcount]] = []
    youtube: Optional[List[Playcount]] = []
    deezer: Optional[List[Playcount]] = []

    master_royalty: Optional[List[MasterRoyalty]] = []
    publishing_royalty: Optional[List[PublishingRoyalty]] = []

    class Config:
        exclude_none = True


class StatsResponse(BaseModel):
    data: PlayCountHistoryAggregated

    class Config:
        exclude_none = True


# Songstats API response schemas
class SongstatsSourceStats(BaseModel):
    """Stats for a specific source (platform)"""
    current: Optional[Dict[str, Any]] = None
    history: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "allow"


class SongstatsTrack(BaseModel):
    """Track information from Songstats"""
    id: Optional[str] = None
    name: Optional[str] = None
    image: Optional[str] = None
    isrc: Optional[str] = None
    spotify_id: Optional[str] = None

    class Config:
        extra = "allow"


class SongstatsHistoricStatsResponse(BaseModel):
    """
    Response from Songstats /tracks/historic_stats endpoint

    Example structure:
    {
        "item": {
            "id": "...",
            "name": "Track Name",
            "image": "...",
            "isrc": "...",
            "spotify_id": "..."
        },
        "spotify": {
            "current": {...},
            "history": [
                {"date": "2024-01-01", "listeners": 1000, "streams": 5000},
                ...
            ]
        },
        "youtube": {
            "current": {...},
            "history": [...]
        }
    }
    """
    item: Optional[SongstatsTrack] = None
    spotify: Optional[SongstatsSourceStats] = None
    apple_music: Optional[SongstatsSourceStats] = None
    youtube: Optional[SongstatsSourceStats] = None
    deezer: Optional[SongstatsSourceStats] = None
    shazam: Optional[SongstatsSourceStats] = None

    class Config:
        extra = "allow"  # Allow additional sources