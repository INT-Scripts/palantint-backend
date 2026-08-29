import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Provenance & Ingestion Governance ────────────────────────────────────────
# Every piece of hydrated (non-manual) data can be traced back to the source
# system and the specific run that wrote it. This replaces the old convention
# of hard-coding "never overwrite manual data" behaviour inside loader scripts.

class DataSource(SQLModel, table=True):
    __tablename__ = "data_sources"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(unique=True, index=True)  # e.g. "trombint", "agenda_ade", "maisel", "groupes", "vault_manual", "admin_panel"
    kind: str  # SCRAPER | MANUAL | ADMIN | USER_SUBMITTED | IMPORT
    label: str
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)


class IngestionRun(SQLModel, table=True):
    __tablename__ = "ingestion_runs"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="data_sources.id", index=True)
    status: str = Field(default="RUNNING")  # RUNNING | SUCCESS | FAILED | PARTIAL
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = Field(default=None)
    records_created: int = Field(default=0)
    records_updated: int = Field(default=0)
    records_deactivated: int = Field(default=0)
    error: Optional[str] = Field(default=None, sa_column=Column(Text))

    source: "DataSource" = Relationship()


# ── Auth ──────────────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class UserCredential(SQLModel, table=True):
    """Encrypted third-party credentials (e.g. CAS) attached to a User.
    Isolated from the `users` table so the presence of a plaintext-sounding
    column can never be mistaken for a plaintext secret again."""
    __tablename__ = "user_credentials"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    provider: str = Field(default="CAS")
    encrypted_username: bytes = Field(sa_column=Column(LargeBinary))
    encrypted_password: bytes = Field(sa_column=Column(LargeBinary))
    updated_at: datetime = Field(default_factory=utc_now, sa_column_kwargs={"onupdate": utc_now})

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_credential_provider"),
    )

    user: "User" = Relationship()


# ── Person (Identity Core) ────────────────────────────────────────────────────
# Replaces `Student` as the universal identity record. Students, professors,
# alumni and external OSINT subjects are all People; role-specific facts live
# in their own tables (memberships, enrollments, housing) instead of being
# bolted onto one giant row.

class Person(SQLModel, table=True):
    __tablename__ = "people"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    kind: str = Field(default="STUDENT", index=True)  # STUDENT | PROFESSOR | ALUMNUS | STAFF | EXTERNAL

    first_name: str = Field(default="")
    last_name: str = Field(default="")
    display_name: Optional[str] = Field(default=None)
    email: Optional[str] = Field(default=None, index=True)
    profile_picture_path: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, index=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now, sa_column_kwargs={"onupdate": utc_now})
    last_seen_at: datetime = Field(default_factory=utc_now)

    identities: list["ExternalIdentity"] = Relationship(
        back_populates="person",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    memberships: list["OrganizationMembership"] = Relationship(
        back_populates="person",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    housing_history: list["PersonHousing"] = Relationship(
        back_populates="person",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    social_links: list["SocialLink"] = Relationship(
        back_populates="person",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    media: list["Media"] = Relationship(
        back_populates="person",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    relationships_as_a: list["PersonRelationship"] = Relationship(
        back_populates="person_a",
        sa_relationship_kwargs={
            "primaryjoin": "Person.id==PersonRelationship.person_a_id",
            "cascade": "all, delete-orphan"
        }
    )
    relationships_as_b: list["PersonRelationship"] = Relationship(
        back_populates="person_b",
        sa_relationship_kwargs={
            "primaryjoin": "Person.id==PersonRelationship.person_b_id",
            "cascade": "all, delete-orphan"
        }
    )


class ExternalIdentity(SQLModel, table=True):
    """Maps a Person to an identifier in some external/source system
    (trombint UID, CAS username, ADE resource id, self-submitted alumni form...).
    Lets a new data source be onboarded without ever touching `people`."""
    __tablename__ = "external_identities"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", index=True)
    source_id: uuid.UUID = Field(foreign_key="data_sources.id", index=True)
    external_id: str = Field(index=True)
    confidence: str = Field(default="CONFIRMED")  # CONFIRMED | LIKELY | UNCONFIRMED
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_identity_source_external_id"),
    )

    person: "Person" = Relationship(back_populates="identities")
    source: "DataSource" = Relationship()


# ── Organizations (Schools, Promos, Class Groups, Clubs, Labs, Companies…) ───
# A single self-referential tree replaces the old separate Club / ClassGroup
# tables and the regex-based hierarchy inference that used to run on every
# sync. New kinds of organizations (labs, partner companies, alumni chapters)
# plug into the same table without new migrations.

class Organization(SQLModel, table=True):
    __tablename__ = "organizations"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    kind: str = Field(index=True)  # SCHOOL | PROGRAM | PROMO | CLASS_GROUP | CLUB | BUREAU | LAB | COMPANY | ADMIN
    name: str = Field(unique=True, index=True)
    slug: Optional[str] = Field(default=None, index=True)
    parent_id: Optional[uuid.UUID] = Field(default=None, foreign_key="organizations.id", index=True)
    location_id: Optional[uuid.UUID] = Field(default=None, foreign_key="locations.id")

    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    logo_url: Optional[str] = Field(default=None)
    color_primary: Optional[str] = Field(default=None)
    color_secondary: Optional[str] = Field(default=None)
    attributes: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now, sa_column_kwargs={"onupdate": utc_now})

    parent: Optional["Organization"] = Relationship(
        sa_relationship_kwargs={"remote_side": "Organization.id"}
    )
    links: list["OrganizationLink"] = Relationship(
        back_populates="organization",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    members: list["OrganizationMembership"] = Relationship(
        back_populates="organization",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class OrganizationLink(SQLModel, table=True):
    __tablename__ = "organization_links"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organizations.id", index=True)
    name: str
    url: str

    organization: "Organization" = Relationship(back_populates="links")


class OrganizationMembership(SQLModel, table=True):
    """Replaces StudentClub + StudentClassGroup. `ended_at` supports history
    (promo changes, club turnover) instead of destructive delete/reinsert."""
    __tablename__ = "organization_memberships"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", index=True)
    organization_id: uuid.UUID = Field(foreign_key="organizations.id", index=True)
    role: str = Field(default="Membre")
    is_mandat: bool = Field(default=False)
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = Field(default=None, index=True)

    person: "Person" = Relationship(back_populates="memberships")
    organization: "Organization" = Relationship(back_populates="members")


# ── Locations (Buildings, Floors, Rooms, Apartments, Laundry Slots) ──────────
# A single hierarchy replaces the previously separate, loosely-typed strings
# used for building/floor/room across ApartmentDetail, AgendaEvent.room,
# LaundrySubscription and MapMetadata.

class Location(SQLModel, table=True):
    __tablename__ = "locations"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    kind: str = Field(index=True)  # BUILDING | FLOOR | ROOM | APARTMENT | MACHINE_SLOT | COMMON_AREA
    code: str = Field(index=True)  # e.g. "U3", "RDC", "1001", "mal-3"
    name: Optional[str] = Field(default=None)
    parent_id: Optional[uuid.UUID] = Field(default=None, foreign_key="locations.id", index=True)
    attributes: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # e.g. for APARTMENT: surface, price, alloc_boursier, alloc_non_boursier, req_b, req_e
    # e.g. for MACHINE_SLOT: machine_type (washer/dryer), pay5vend_ref

    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now, sa_column_kwargs={"onupdate": utc_now})

    __table_args__ = (
        UniqueConstraint("parent_id", "kind", "code", name="uq_location_parent_kind_code"),
    )

    parent: Optional["Location"] = Relationship(
        sa_relationship_kwargs={"remote_side": "Location.id"}
    )


class PersonHousing(SQLModel, table=True):
    """Replaces the single Student.apartment string. Keeps move history."""
    __tablename__ = "person_housing"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", index=True)
    location_id: uuid.UUID = Field(foreign_key="locations.id", index=True)
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = Field(default=None, index=True)

    person: "Person" = Relationship(back_populates="housing_history")
    location: "Location" = Relationship()


# ── Events & Calendars ────────────────────────────────────────────────────────
# Generalizes AgendaEvent so the same table can host ADE courses, club-run
# events, and (per the roadmap) iCal imports / community-submitted events.

class Event(SQLModel, table=True):
    __tablename__ = "events"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    external_ref: Optional[str] = Field(default=None, unique=True, index=True)
    calendar_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(default="COURSE")  # COURSE | EXAM | CLUB | EXTERNAL | PERSONAL
    name: str
    start_time: datetime = Field(index=True)
    end_time: datetime
    location_id: Optional[uuid.UUID] = Field(default=None, foreign_key="locations.id")
    organization_id: Optional[uuid.UUID] = Field(default=None, foreign_key="organizations.id")
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    presenters_raw: Optional[str] = Field(default=None, sa_column=Column(Text))  # unresolved-name fallback
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")

    organization: Optional["Organization"] = Relationship()
    location: Optional["Location"] = Relationship()


class EventOrganization(SQLModel, table=True):
    """Secondary organization links for an event (a course shared by several
    class groups), beyond the single primary Event.organization_id."""
    __tablename__ = "event_organizations"
    event_id: uuid.UUID = Field(foreign_key="events.id", primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organizations.id", primary_key=True)


class EventAttendee(SQLModel, table=True):
    __tablename__ = "event_attendees"
    event_id: uuid.UUID = Field(foreign_key="events.id", primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", primary_key=True)


class EventPresenter(SQLModel, table=True):
    """Structured link for professors/organizers once resolved to a Person,
    complementing Event.presenters_raw for names that couldn't be matched."""
    __tablename__ = "event_presenters"
    event_id: uuid.UUID = Field(foreign_key="events.id", primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", primary_key=True)
    role: str = Field(default="Professeur")


# ── Human Intelligence (OSINT Research) ──────────────────────────────────────

class SocialLink(SQLModel, table=True):
    __tablename__ = "social_links"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", index=True)
    platform: str
    username: str
    url: str
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")
    confidence: str = Field(default="CONFIRMED")  # CONFIRMED | LIKELY | UNCONFIRMED

    __table_args__ = (
        UniqueConstraint("person_id", "platform", "username", name="uq_social_link_identity"),
    )

    person: "Person" = Relationship(back_populates="social_links")


class RelationshipType(SQLModel, table=True):
    __tablename__ = "relationship_types"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True)
    color: str = Field(default="#cccccc")
    is_symmetric: bool = Field(default=True)  # False for directional types (e.g. "Mentor de")

    relationships: list["PersonRelationship"] = Relationship(
        back_populates="relationship_type",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class PersonRelationship(SQLModel, table=True):
    __tablename__ = "person_relationships"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_a_id: uuid.UUID = Field(foreign_key="people.id")
    person_b_id: uuid.UUID = Field(foreign_key="people.id")
    relationship_type_id: uuid.UUID = Field(foreign_key="relationship_types.id")
    confidence: str = Field(default="LIKELY")  # CONFIRMED | LIKELY | UNCONFIRMED
    evidence_media_id: Optional[uuid.UUID] = Field(default=None, foreign_key="media.id")
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")
    created_at: datetime = Field(default_factory=utc_now)

    person_a: "Person" = Relationship(
        sa_relationship_kwargs={"primaryjoin": "PersonRelationship.person_a_id==Person.id"},
        back_populates="relationships_as_a"
    )
    person_b: "Person" = Relationship(
        sa_relationship_kwargs={"primaryjoin": "PersonRelationship.person_b_id==Person.id"},
        back_populates="relationships_as_b"
    )
    relationship_type: "RelationshipType" = Relationship(back_populates="relationships")


class Media(SQLModel, table=True):
    __tablename__ = "media"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    person_id: uuid.UUID = Field(foreign_key="people.id", index=True)
    kind: str = Field(default="NOTE")  # IMAGE | VIDEO | NOTE | DOCUMENT
    file_path: Optional[str] = Field(default=None)
    content: Optional[str] = Field(default=None, sa_column=Column(Text))
    author_name: Optional[str] = Field(default=None)
    uploaded_by_user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id")
    source_id: Optional[uuid.UUID] = Field(default=None, foreign_key="data_sources.id")
    uploaded_at: datetime = Field(default_factory=utc_now)

    person: "Person" = Relationship(back_populates="media")
    uploader: Optional["User"] = Relationship()


# ── User-Facing Features ─────────────────────────────────────────────────────

class RecentlyViewed(SQLModel, table=True):
    __tablename__ = "recently_viewed"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    person_id: uuid.UUID = Field(foreign_key="people.id")
    viewed_at: datetime = Field(default_factory=utc_now)

    user: "User" = Relationship()
    person: "Person" = Relationship()


class LaundrySubscription(SQLModel, table=True):
    __tablename__ = "laundry_subscriptions"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    location_id: uuid.UUID = Field(foreign_key="locations.id", index=True)  # kind=MACHINE_SLOT
    created_at: datetime = Field(default_factory=utc_now)
    is_active: bool = Field(default=True, index=True)

    user: "User" = Relationship()
    location: "Location" = Relationship()


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(sa_column=Column(PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), index=True))
    kind: str = Field(default="GENERIC")  # GENERIC | LAUNDRY_READY | SCHEDULE_CHANGE | RELATIONSHIP_ADDED …
    title: str
    message: str
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    is_read: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)

    user: "User" = Relationship()


# ── Maps & 3D Rendering ───────────────────────────────────────────────────────

class ThreeDConfig(SQLModel, table=True):
    __tablename__ = "three_d_config"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    key: str = Field(default="default", unique=True, index=True)
    tile_mappings: Dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    markers: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utc_now)


class MapMetadata(SQLModel, table=True):
    __tablename__ = "maps_metadata"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    location_id: uuid.UUID = Field(foreign_key="locations.id", unique=True, index=True)  # kind=FLOOR
    pillars: List[Dict[str, float]] = Field(default_factory=list, sa_column=Column(JSON))

    location: "Location" = Relationship()


# Alias Base to SQLModel to avoid breaking alembic environment file expectations immediately
Base = SQLModel
