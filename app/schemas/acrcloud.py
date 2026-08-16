from pydantic import BaseModel

from typing import Literal, Dict, Any, List


class Result(BaseModel):

    type: Literal["music", "cover_song"]
    artist_name: str
    song_name: str
    duration: float
    isrc: str

    # validate URLs?
    spotify_link: str
    applemusic_link: str
    deezer_link: str
    youtube_link: str


class Song(BaseModel):

    filename: str
    file_id: str
    loading: bool
    tracks: List[Result]


class ShowContainerResponse(BaseModel):

    songs: List[Song]
    current_page: int
    last_page: int
    per_page: int
