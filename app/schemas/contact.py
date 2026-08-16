from pydantic import BaseModel


class ClaimRequest(BaseModel):
    artist: str
    title: str
    message: str
