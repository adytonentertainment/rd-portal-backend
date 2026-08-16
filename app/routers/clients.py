from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_session
from app.models.models import User, Client, Subscription, SubscriptionTier
from app.routers.auth import get_user
from app.settings.settings import get_settings

settings = get_settings()

# Tiers that can have multiple clients
MULTI_CLIENT_TIERS = {SubscriptionTier.ELITE, SubscriptionTier.ENTERPRISE}

clients_router = APIRouter(
    prefix="/clients",
    tags=["Clients"],
)


# Pydantic models
class ClientCreate(BaseModel):
    name: str
    avatar_url: Optional[str] = None
    color: Optional[str] = "#3b82f6"
    writer_ipi: Optional[str] = None
    writer_name: Optional[str] = None
    publisher_ipi: Optional[str] = None
    publisher_name: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    color: Optional[str] = None
    writer_ipi: Optional[str] = None
    writer_name: Optional[str] = None
    publisher_ipi: Optional[str] = None
    publisher_name: Optional[str] = None


class ClientResponse(BaseModel):
    id: int
    name: str
    avatar_url: Optional[str]
    color: str
    writer_ipi: Optional[str]
    writer_name: Optional[str]
    publisher_ipi: Optional[str]
    publisher_name: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClientListResponse(BaseModel):
    clients: List[ClientResponse]
    total: int


@clients_router.get("", response_model=ClientListResponse)
def get_clients(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get all clients for the current user"""
    clients = session.query(Client).filter(Client.user_id == user.id).order_by(Client.name).all()
    return ClientListResponse(
        clients=[ClientResponse.model_validate(c) for c in clients],
        total=len(clients),
    )


@clients_router.get("/{client_id}", response_model=ClientResponse)
def get_client(
    client_id: int,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get a specific client"""
    client = session.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return ClientResponse.model_validate(client)


@clients_router.post("", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
def create_client(
    request: ClientCreate,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Create a new client"""
    # Check subscription tier for multi-client access
    subscription = session.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.stripe_mode == settings.stripe_mode
    ).first()

    user_tier = subscription.tier if subscription else SubscriptionTier.FREE

    # Non-Elite/Enterprise users can only have one client
    if user_tier not in MULTI_CLIENT_TIERS:
        existing_client_count = session.query(Client).filter(Client.user_id == user.id).count()
        if existing_client_count >= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Multiple clients require an Elite or Enterprise subscription. Upgrade to add more clients.",
            )

    # Check if client with same name already exists for this user
    existing = session.query(Client).filter(Client.user_id == user.id, Client.name == request.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A client with this name already exists",
        )

    client = Client(
        user_id=user.id,
        name=request.name,
        avatar_url=request.avatar_url,
        color=request.color or "#3b82f6",
        writer_ipi=request.writer_ipi,
        writer_name=request.writer_name,
        publisher_ipi=request.publisher_ipi,
        publisher_name=request.publisher_name,
    )
    session.add(client)
    session.commit()
    session.refresh(client)

    return ClientResponse.model_validate(client)


@clients_router.put("/{client_id}", response_model=ClientResponse)
def update_client(
    client_id: int,
    request: ClientUpdate,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Update a client"""
    client = session.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

    # Check for duplicate name if name is being changed
    if request.name and request.name != client.name:
        existing = session.query(Client).filter(
            Client.user_id == user.id, Client.name == request.name, Client.id != client_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A client with this name already exists",
            )

    if request.name is not None:
        client.name = request.name
    if request.avatar_url is not None:
        client.avatar_url = request.avatar_url
    if request.color is not None:
        client.color = request.color
    if request.writer_ipi is not None:
        client.writer_ipi = request.writer_ipi
    if request.writer_name is not None:
        client.writer_name = request.writer_name
    if request.publisher_ipi is not None:
        client.publisher_ipi = request.publisher_ipi
    if request.publisher_name is not None:
        client.publisher_name = request.publisher_name

    client.updated_at = datetime.now()
    session.commit()
    session.refresh(client)

    return ClientResponse.model_validate(client)


@clients_router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(
    client_id: int,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Delete a client (catalog items and statements will have client_id set to NULL)"""
    client = session.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

    session.delete(client)
    session.commit()
    return None
