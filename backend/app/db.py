"""Event log of record: schema and the queries the rest of the backend needs.

SQLite by default (WAL mode), Postgres via DATABASE_URL=postgresql+asyncpg://...
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    and_,
    cast,
    delete,
    event,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC; always hands back timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One row per thing that happened: a tracked object, an arm/disarm, a zone opening..."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_events_source_external"),
        Index("ix_events_start_id", "start_time", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # frigate | alarm | sensor
    type: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(128))  # display name: camera, zone or panel
    subject_id: Mapped[str] = mapped_column(String(128), index=True)  # stable id for filtering
    label: Mapped[str | None] = mapped_column(String(64), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    external_id: Mapped[str | None] = mapped_column(String(64))
    zones: Mapped[list] = mapped_column(JSONType, default=list)
    has_clip: Mapped[bool] = mapped_column(Boolean, default=False)
    has_snapshot: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_path: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)


class AlarmStateHistory(Base):
    __tablename__ = "alarm_state_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    panel_id: Mapped[str] = mapped_column(String(128), index=True)
    partition: Mapped[str | None] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32))
    previous_state: Mapped[str | None] = mapped_column(String(32))
    triggered_by: Mapped[str | None] = mapped_column(String(512))


class ZoneStateHistory(Base):
    __tablename__ = "zone_state_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    zone_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    faulted: Mapped[bool | None] = mapped_column(Boolean)
    low_battery: Mapped[bool | None] = mapped_column(Boolean)
    tampered: Mapped[bool | None] = mapped_column(Boolean)
    # Not exposed by Home Assistant's Total Connect integration today; kept so a future
    # direct total_connect_client source can fill them without a schema change.
    battery_level: Mapped[int | None] = mapped_column(Integer)
    signal_strength: Mapped[int | None] = mapped_column(Integer)


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def event_to_dict(row: Event) -> dict[str, Any]:
    is_frigate = row.source == "frigate"
    has_snapshot = bool(row.has_snapshot or row.snapshot_path)
    return {
        "id": row.id,
        "source": row.source,
        "type": row.type,
        "subject": row.subject,
        "subject_id": row.subject_id,
        "label": row.label,
        "confidence": row.confidence,
        "severity": row.severity,
        "start_time": iso(row.start_time),
        "end_time": iso(row.end_time),
        "in_progress": is_frigate and row.end_time is None,
        "zones": row.zones or [],
        "has_clip": bool(row.has_clip),
        "has_snapshot": has_snapshot,
        "snapshot_url": f"/api/events/{row.id}/snapshot" if has_snapshot else None,
        "thumbnail_url": f"/api/events/{row.id}/thumbnail" if is_frigate else None,
        "clip_url": f"/api/events/{row.id}/clip" if row.has_clip else None,
        "extra": row.extra or {},
    }


@dataclass
class EventFilter:
    sources: list[str] | None = None
    types: list[str] | None = None
    severities: list[str] | None = None
    subject_id: str | None = None
    label: str | None = None
    q: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    cursor: str | None = None
    limit: int = 50


def encode_cursor(row: Event) -> str:
    raw = f"{row.start_time.isoformat()}|{row.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    padded = cursor + "=" * (-len(cursor) % 4)
    ts, _, row_id = base64.urlsafe_b64decode(padded).decode().partition("|")
    return datetime.fromisoformat(ts), int(row_id)


class Store:
    def __init__(self, url: str):
        connect_args = {"timeout": 30} if url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(url, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # --- writes -------------------------------------------------------------------------

    async def add_event(self, **fields: Any) -> Event:
        async with self.sessions() as session:
            row = Event(**fields)
            session.add(row)
            await session.commit()
            return row

    async def upsert_external_event(self, source: str, external_id: str, fields: dict[str, Any]) -> tuple[Event, bool]:
        """Insert or update the row for an upstream id (Frigate's tracked-object id)."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(Event).where(Event.source == source, Event.external_id == external_id)
            )
            created = row is None
            if created:
                row = Event(source=source, external_id=external_id, **fields)
                session.add(row)
            else:
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.commit()
            return row, created

    async def set_snapshot_path(self, event_id: int, path: str | None) -> None:
        async with self.sessions() as session:
            await session.execute(update(Event).where(Event.id == event_id).values(snapshot_path=path))
            await session.commit()

    async def add_alarm_history(self, **fields: Any) -> None:
        async with self.sessions() as session:
            session.add(AlarmStateHistory(**fields))
            await session.commit()

    async def add_zone_history(self, **fields: Any) -> None:
        async with self.sessions() as session:
            session.add(ZoneStateHistory(**fields))
            await session.commit()

    # --- reads --------------------------------------------------------------------------

    async def get_event(self, event_id: int) -> Event | None:
        async with self.sessions() as session:
            return await session.get(Event, event_id)

    async def get_external_event(self, source: str, external_id: str) -> Event | None:
        async with self.sessions() as session:
            return await session.scalar(select(Event).where(Event.source == source, Event.external_id == external_id))

    async def query_events(self, f: EventFilter) -> tuple[list[Event], str | None]:
        stmt = select(Event)
        if f.sources:
            stmt = stmt.where(Event.source.in_(f.sources))
        if f.types:
            stmt = stmt.where(Event.type.in_(f.types))
        if f.severities:
            stmt = stmt.where(Event.severity.in_(f.severities))
        if f.subject_id:
            stmt = stmt.where(Event.subject_id == f.subject_id)
        if f.label:
            stmt = stmt.where(Event.label == f.label)
        if f.q:
            like = f"%{f.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Event.subject).like(like),
                    func.lower(Event.label).like(like),
                    func.lower(Event.type).like(like),
                    func.lower(cast(Event.zones, String)).like(like),
                )
            )
        if f.since:
            stmt = stmt.where(Event.start_time >= f.since)
        if f.until:
            stmt = stmt.where(Event.start_time < f.until)
        if f.cursor:
            ts, row_id = decode_cursor(f.cursor)
            stmt = stmt.where(or_(Event.start_time < ts, and_(Event.start_time == ts, Event.id < row_id)))
        limit = max(1, min(f.limit, 200))
        stmt = stmt.order_by(Event.start_time.desc(), Event.id.desc()).limit(limit + 1)
        async with self.sessions() as session:
            rows = list((await session.scalars(stmt)).all())
        next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
        return rows[:limit], next_cursor

    async def facets(self) -> dict[str, Any]:
        async with self.sessions() as session:
            subjects = (
                await session.execute(
                    select(Event.source, Event.subject_id, func.max(Event.subject))
                    .group_by(Event.source, Event.subject_id)
                    .order_by(Event.source, Event.subject_id)
                )
            ).all()
            labels = (
                await session.scalars(
                    select(Event.label)
                    .where(Event.source == "frigate", Event.label.is_not(None))
                    .distinct()
                    .order_by(Event.label)
                )
            ).all()
        return {
            "subjects": [{"source": s, "id": sid, "name": name} for s, sid, name in subjects],
            "labels": list(labels),
        }

    async def count_events(self) -> int:
        async with self.sessions() as session:
            return int(await session.scalar(select(func.count(Event.id))) or 0)

    async def alarm_history(self, limit: int = 100) -> list[AlarmStateHistory]:
        async with self.sessions() as session:
            stmt = select(AlarmStateHistory).order_by(AlarmStateHistory.timestamp.desc()).limit(limit)
            return list((await session.scalars(stmt)).all())

    async def zone_history(self, zone_id: str, limit: int = 100) -> list[ZoneStateHistory]:
        async with self.sessions() as session:
            stmt = (
                select(ZoneStateHistory)
                .where(ZoneStateHistory.zone_id == zone_id)
                .order_by(ZoneStateHistory.timestamp.desc())
                .limit(limit)
            )
            return list((await session.scalars(stmt)).all())

    async def last_alarm_states(self) -> dict[str, str]:
        """Most recent recorded state per panel, so restarts don't log phantom changes."""
        latest = (
            select(AlarmStateHistory.panel_id, func.max(AlarmStateHistory.id).label("max_id"))
            .group_by(AlarmStateHistory.panel_id)
            .subquery()
        )
        stmt = select(AlarmStateHistory).join(latest, AlarmStateHistory.id == latest.c.max_id)
        async with self.sessions() as session:
            return {row.panel_id: row.state for row in (await session.scalars(stmt)).all()}

    async def last_zone_states(self) -> dict[str, ZoneStateHistory]:
        latest = (
            select(ZoneStateHistory.zone_id, func.max(ZoneStateHistory.id).label("max_id"))
            .group_by(ZoneStateHistory.zone_id)
            .subquery()
        )
        stmt = select(ZoneStateHistory).join(latest, ZoneStateHistory.id == latest.c.max_id)
        async with self.sessions() as session:
            return {row.zone_id: row for row in (await session.scalars(stmt)).all()}

    # --- retention ----------------------------------------------------------------------

    async def expired_snapshots(self, older_than_days: int) -> list[tuple[int, str]]:
        cutoff = utcnow() - timedelta(days=older_than_days)
        async with self.sessions() as session:
            rows = await session.execute(
                select(Event.id, Event.snapshot_path).where(
                    Event.snapshot_path.is_not(None), Event.start_time < cutoff
                )
            )
            return [(row_id, path) for row_id, path in rows.all()]

    async def purge_events(self, older_than_days: int) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        async with self.sessions() as session:
            result = await session.execute(delete(Event).where(Event.start_time < cutoff))
            await session.execute(delete(AlarmStateHistory).where(AlarmStateHistory.timestamp < cutoff))
            await session.execute(delete(ZoneStateHistory).where(ZoneStateHistory.timestamp < cutoff))
            await session.commit()
            return result.rowcount or 0


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
