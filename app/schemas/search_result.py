from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class SearchResult(BaseModel):
    id: Optional[int] = None
    artist: str
    title: str
    album: str
    date_added: datetime
    is_infringement: bool
    isrc: str
    album_art: str
    spotify_track_id: str
    spotify_popularity: Optional[int] = 0
    publishing_royalty: Optional[float] = None
    master_royalty: Optional[float] = None
