import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)



# ── Auth ────────────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class RecentlyViewed(SQLModel, table=True):
    __tablename__ = "recently_viewed"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    student_id: uuid.UUID = Field(foreign_key="students.id")
    viewed_at: datetime = Field(default_factory=utc_now)

    user: "User" = Relationship()
    student: "Student" = Relationship()


# ── Junction Tables ─────────────────────────────────────────────────────────

class StudentClub(SQLModel, table=True):
    __tablename__ = "student_clubs"
    student_id: uuid.UUID = Field(foreign_key="students.id", primary_key=True)
    club_id: uuid.UUID = Field(foreign_key="clubs.id", primary_key=True)
    role: str = Field(default="Membre")
    is_mandat: bool = Field(default=False)

    student: "Student" = Relationship(back_populates="clubs")
    club: "Club" = Relationship(back_populates="members")


class StudentClassGroup(SQLModel, table=True):
    __tablename__ = "student_class_groups"
    student_id: uuid.UUID = Field(foreign_key="students.id", primary_key=True)
    class_group_id: uuid.UUID = Field(foreign_key="class_groups.id", primary_key=True)
    role: str = Field(default="Membre")

    student: "Student" = Relationship(back_populates="class_groups")
    class_group: "ClassGroup" = Relationship(back_populates="members")


class StudentAgendaEvent(SQLModel, table=True):
    __tablename__ = "student_agenda_events"
    student_id: uuid.UUID = Field(foreign_key="students.id", primary_key=True)
    event_id: uuid.UUID = Field(foreign_key="agenda_events.id", primary_key=True)

    student: "Student" = Relationship(back_populates="agenda_events")
    event: "AgendaEvent" = Relationship(back_populates="students")


class EventClassGroup(SQLModel, table=True):
    __tablename__ = "event_class_groups"
    event_id: uuid.UUID = Field(foreign_key="agenda_events.id", primary_key=True)
    class_group_id: uuid.UUID = Field(foreign_key="class_groups.id", primary_key=True)

    event: "AgendaEvent" = Relationship(back_populates="class_groups")
    class_group: "ClassGroup" = Relationship(back_populates="events")



class StudentRelationship(SQLModel, table=True):
    __tablename__ = "student_relationships"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    student_a_id: uuid.UUID = Field(foreign_key="students.id")
    student_b_id: uuid.UUID = Field(foreign_key="students.id")
    relationship_type_id: uuid.UUID = Field(foreign_key="relationship_types.id")
    created_at: datetime = Field(default_factory=utc_now)

    student_a: "Student" = Relationship(
        sa_relationship_kwargs={"primaryjoin": "StudentRelationship.student_a_id==Student.id"},
        back_populates="relationships_as_a"
    )
    student_b: "Student" = Relationship(
        sa_relationship_kwargs={"primaryjoin": "StudentRelationship.student_b_id==Student.id"},
        back_populates="relationships_as_b"
    )
    relationship_type: "RelationshipType" = Relationship(back_populates="relationships")


# ── Core Entities ───────────────────────────────────────────────────────────

class Student(SQLModel, table=True):
    __tablename__ = "students"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Unique external identifier from TrombINT
    trombint_id: str = Field(unique=True, index=True)

    # Identity
    first_name: str = Field(default="")
    last_name: str = Field(default="")
    email: Optional[str] = Field(default=None, index=True)
    profile_picture_path: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, index=True)

    # Academic
    ecole: Optional[str] = Field(default=None)       # e.g. "Télécom SudParis", "IMT-BS"
    promo: Optional[str] = Field(default=None)        # e.g. "Ingénieur 1ère année"

    # Housing
    apartment: Optional[str] = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(
        default_factory=utc_now,
        sa_column_kwargs={"onupdate": utc_now}
    )
    last_seen_at: datetime = Field(default_factory=utc_now)

    # Relationships
    social_links: list["SocialLink"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    clubs: list["StudentClub"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    class_groups: list["StudentClassGroup"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    media: list["Media"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    agenda_events: list["StudentAgendaEvent"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    relationships_as_a: list["StudentRelationship"] = Relationship(
        back_populates="student_a",
        sa_relationship_kwargs={
            "primaryjoin": "Student.id==StudentRelationship.student_a_id",
            "cascade": "all, delete-orphan"
        }
    )
    relationships_as_b: list["StudentRelationship"] = Relationship(
        back_populates="student_b",
        sa_relationship_kwargs={
            "primaryjoin": "Student.id==StudentRelationship.student_b_id",
            "cascade": "all, delete-orphan"
        }
    )


class SocialLink(SQLModel, table=True):
    __tablename__ = "social_links"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    student_id: uuid.UUID = Field(foreign_key="students.id")
    platform: str
    username: str
    url: str

    student: "Student" = Relationship(back_populates="social_links")


class ClubLink(SQLModel, table=True):
    __tablename__ = "club_links"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    club_id: uuid.UUID = Field(foreign_key="clubs.id")
    name: str
    url: str

    club: "Club" = Relationship(back_populates="links")


class Club(SQLModel, table=True):
    __tablename__ = "clubs"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True)
    slug: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    logo_url: Optional[str] = Field(default=None)
    type: Optional[str] = Field(default=None)                      # e.g. "Club", "Bureau"
    association_of_origin: Optional[str] = Field(default=None)     # e.g. "BDE", "BDA", "ASINT"
    color_primary: Optional[str] = Field(default=None)
    color_secondary: Optional[str] = Field(default=None)

    members: list["StudentClub"] = Relationship(
        back_populates="club",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    events: list["AgendaEvent"] = Relationship(
        back_populates="club",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    links: list["ClubLink"] = Relationship(
        back_populates="club",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class ClassGroup(SQLModel, table=True):
    __tablename__ = "class_groups"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True, index=True)

    members: list["StudentClassGroup"] = Relationship(
        back_populates="class_group",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    events: list["EventClassGroup"] = Relationship(
        back_populates="class_group",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class RelationshipType(SQLModel, table=True):
    __tablename__ = "relationship_types"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True)
    color: str = Field(default="#cccccc")

    relationships: list["StudentRelationship"] = Relationship(
        back_populates="relationship_type",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class Media(SQLModel, table=True):
    __tablename__ = "media"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    student_id: uuid.UUID = Field(foreign_key="students.id")
    type: str  # IMAGE, VIDEO, NOTE
    file_path: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default=None, sa_column=Column(Text))
    author_name: Optional[str] = Field(default=None)
    uploaded_by_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id")
    uploaded_at: datetime = Field(default_factory=utc_now)

    student: "Student" = Relationship(back_populates="media")
    uploader: Optional["User"] = Relationship()


class AgendaEvent(SQLModel, table=True):
    __tablename__ = "agenda_events"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    event_ref_id: str = Field(unique=True, index=True)
    calendar_id: str = Field(index=True)
    name: str
    type: str
    start_time: datetime = Field(index=True)
    end_time: datetime
    room: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    professors: Optional[str] = Field(default=None, sa_column=Column(Text))
    club_id: Optional[uuid.UUID] = Field(default=None, foreign_key="clubs.id")

    students: list["StudentAgendaEvent"] = Relationship(
        back_populates="event",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    club: Optional["Club"] = Relationship(back_populates="events")
    class_groups: list["EventClassGroup"] = Relationship(
        back_populates="event",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class MapMetadata(SQLModel, table=True):
    __tablename__ = "maps_metadata"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    building_id: str = Field(index=True)
    floor_id: str = Field(index=True)
    pillars: List[Dict[str, float]] = Field(default_factory=list, sa_column=Column(JSON))

    __table_args__ = (
        UniqueConstraint("building_id", "floor_id", name="uq_building_floor"),
    )


class ApartmentDetail(SQLModel, table=True):
    __tablename__ = "apartment_details"
    id: str = Field(primary_key=True)  # Room number (e.g., "1001")
    building: str = Field(index=True)  # Building (e.g., "U1")
    floor: str = Field(index=True)     # Floor name (e.g., "Rez de Chaussée")
    type: Optional[str] = Field(default=None)
    surface: Optional[str] = Field(default=None)
    price: Optional[str] = Field(default=None)
    alloc_boursier: Optional[str] = Field(default=None)
    alloc_non_boursier: Optional[str] = Field(default=None)
    req_b: int = Field(default=0)
    req_e: str = Field(default="")

class LaundrySubscription(SQLModel, table=True):
    __tablename__ = "laundry_subscriptions"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    building: str = Field(index=True)
    machine_nbr: int = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    is_active: bool = Field(default=True, index=True)

    user: "User" = Relationship()


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    title: str
    message: str
    is_read: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)

    user: "User" = Relationship()


# Alias Base to SQLModel to avoid breaking alembic environment file expectations immediately
Base = SQLModel
