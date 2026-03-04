import uuid
from datetime import datetime
from typing import List, Optional

from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, String

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class RecentlyViewed(SQLModel, table=True):
    __tablename__ = "recently_viewed"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    student_id: uuid.UUID = Field(foreign_key="students.id")
    viewed_at: datetime = Field(default_factory=datetime.utcnow)

    # Note: user and student relations will need linking.
