from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


class TrackHistory(BaseModel):
    date: datetime
    spotify_stream_count: Optional[int]

    # this needs discussion
    youtube_stream_count: Optional[int]
    deezer_stream_count: Optional[int]
    applemusic_stream_count: Optional[int]


class Track(BaseModel):
    id: int
    artist: str
    title: str
    album: str
    duration: int
    date_added: datetime
    isrc: str
    user_id: int
    is_infringement: bool
    album_art: str
    spotify_track_id: Optional[str]
    publishing_royalty: Optional[float] = None
    master_royalty: Optional[float] = None
    case_status: Optional[str] = None
    pro: Optional[str] = None
    publisher_type: Optional[str] = None
    spotify_playcount: Optional[int] = None
    youtube_playcount: Optional[int] = None
    playcount: Optional[int] = None


class TrackResponse(BaseModel):
    href: str
    items: List[Track]
    total: int


class TrackUpdate(BaseModel):
    artist: str = Field(default=None)
    title: str = Field(default=None)
    album: str = Field(default=None)
    duration: int = Field(default=None)
    publishing_royalty: float = Field(default=None)
    master_royalty: float = Field(default=None)
    date_added: datetime = Field(default=None)
    isrc: str = Field(default=None)
    is_infringement: bool = Field(default=None)
    album_art: str = Field(default=None)
    case_status: str = Field(default=None)
    pro: str = Field(default=None)
    publisher_type: str = Field(default=None)


class BulkUpdateRequest(BaseModel):
    """Request schema for bulk updating tracks"""
    track_ids: List[str]  # Can be numeric UserCatalog IDs or spotify_track_ids
    updates: TrackUpdate


class BulkUpdateResponse(BaseModel):
    """Response schema for bulk update operation"""
    updated_count: int
    failed_ids: List[str]
    message: str


class BulkAddSong(BaseModel):
    """Schema for a song from revenue statements to be added to catalog"""
    title: str
    artist: str
    isrc: Optional[str] = ""
    upc: Optional[str] = ""


class BulkAddRequest(BaseModel):
    """Request schema for bulk adding songs from revenue statements"""
    songs: List[BulkAddSong]


class BulkAddResponse(BaseModel):
    """Response schema for bulk add operation"""
    added: int
    skipped: int
    skipped_items: List[dict]
    message: str
