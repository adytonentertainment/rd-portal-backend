from pydantic import BaseModel
from datetime import date
from typing import List, Union

class TrackInfo(BaseModel):
    songstats_track_id: str
    avatar: str
    site_url: str
    release_date: date
    title: str
    artists: List
    labels: List

class YoutubeHistoryPoint(BaseModel):
    date: date
    video_views_total: int
    video_likes_total: int
    video_comments_total: int
    shorts_total: int

class SpotifyHistoryPoint(BaseModel):
    date: date
    streams_total: int
    popularity_current: int

class HistoryData(BaseModel):
    history: List[Union[YoutubeHistoryPoint, SpotifyHistoryPoint]]

class HistorySource(BaseModel):
    source: str
    data: HistoryData

class HistoricStatsResponse(BaseModel):
    result: str
    message: str
    stats: List[HistorySource]
    track_info: TrackInfo