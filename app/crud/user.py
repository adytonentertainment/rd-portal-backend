from fastapi import HTTPException
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from ..database import get_session
from ..models import User

bcrypt_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_user(username: str, email: str, password: str, session: Session):

    # now create the user
    user = User(
        username=username, email=email, hashed_password=bcrypt_context.hash(password)
    )
    session.add(user)
    session.commit()


def get_user(username: str, password: str):
    pass
