from typing import Generator
from .database import SessionLocal


# this create a connection to the database and closes it on garbage collection
# with this, we do not need to close the db connection manually
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
