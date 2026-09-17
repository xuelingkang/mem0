import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    key_prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    auth_type: Mapped[str] = mapped_column(String(32), default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RefreshTokenJti(Base):
    __tablename__ = "refresh_token_jtis"

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )


class DreamRun(Base):
    """Dream 运行审计（设计 §4.3.1）。

    一轮整合一行，记录统计口径与报告位置；不参与任何判定，只服务可观测性。
    """

    __tablename__ = "dream_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    mode: Mapped[str] = mapped_column(String(16))  # dry_run / live
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # completed / failed / timeout / skipped（lock 未获取）
    status: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[int] = mapped_column(Integer, default=0)
    clusters: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    failed_clusters: Mapped[int] = mapped_column(Integer, default=0)
    observations_written: Mapped[int] = mapped_column(Integer, default=0)
    observations_superseded: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class DreamClusterState(Base):
    """已判定的簇（设计 §4.3.2）。

    一行代表「这个成员集合已被判定过」，用于下一轮的成本跳过；行只增不删，同一
    key 再次判定时更新 `last_evaluated_at` / `evaluations` / `decision`。表数据丢失
    只导致重复调用 LLM，不改变整合结果。
    """

    __tablename__ = "dream_cluster_states"

    observation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_user_id: Mapped[str] = mapped_column(String(255), index=True)
    scope_agent_id: Mapped[str] = mapped_column(String(255), index=True)
    # written / no_higher_order_pattern / unresolvable_sources / insufficient_evidence / duplicate_text
    decision: Mapped[str] = mapped_column(String(32))
    observed_point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 与 observation_key 恒等，保留为显式字段便于排查（不引入第二套指纹）。
    member_ids_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    evaluations: Mapped[int] = mapped_column(Integer, default=1)
