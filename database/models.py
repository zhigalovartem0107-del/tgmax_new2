"""ORM models."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, DateTime, MetaData, String, TypeDecorator
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime on every backend.

    SQLite has no timezone support and returns naive datetimes, which would
    break comparisons with aware values such as utcnow().
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime given; use an aware UTC datetime")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(AsyncAttrs, DeclarativeBase):
    # Stable constraint names, so future Alembic migrations can alter them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {datetime: UTCDateTime}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="balance_non_negative"),)

    # MAX user_id (int64), used directly as the primary key.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(256), default="")
    last_name: Mapped[str | None] = mapped_column(String(256))

    # Integer units only, never float. Whether a unit is a credit or a kopeck
    # is decided together with the pricing model.
    balance: Mapped[int] = mapped_column(default=0)

    subscription_plan: Mapped[str | None] = mapped_column(String(32))
    subscription_expires_at: Mapped[datetime | None]

    is_banned: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def has_active_subscription(self) -> bool:
        return (
            self.subscription_plan is not None
            and self.subscription_expires_at is not None
            and self.subscription_expires_at > utcnow()
        )

    def __repr__(self) -> str:
        return f"User(id={self.id}, balance={self.balance}, plan={self.subscription_plan!r})"
