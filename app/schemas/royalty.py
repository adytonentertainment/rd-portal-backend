from pydantic import BaseModel


class RoyaltyRequest(BaseModel):
    royalty_per_stream: float


class RoyaltyResponse(BaseModel):
    royalty_per_stream: float
