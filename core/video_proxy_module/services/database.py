"""Database initialization and connection management for video proxy module."""

import os
import logging
from typing import Optional
from pydal import DAL, Field

logger = logging.getLogger(__name__)

_db_instance: Optional[DAL] = None


def get_db() -> DAL:
    """Get DAL database instance."""
    global _db_instance

    if _db_instance is None:
        db_type = os.getenv("DB_TYPE", "sqlite")

        if db_type == "postgres":
            db_host = os.getenv("DB_HOST", "localhost")
            db_port = os.getenv("DB_PORT", "5432")
            db_name = os.getenv("DB_NAME", "waddlebot")
            db_user = os.getenv("DB_USER", "waddlebot")
            db_pass = os.getenv("DB_PASS", "")
            db_uri = f"postgres://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        elif db_type == "mysql":
            db_host = os.getenv("DB_HOST", "localhost")
            db_port = os.getenv("DB_PORT", "3306")
            db_name = os.getenv("DB_NAME", "waddlebot")
            db_user = os.getenv("DB_USER", "waddlebot")
            db_pass = os.getenv("DB_PASS", "")
            db_uri = f"mysql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
        else:
            db_path = os.getenv("DB_PATH", "/app/data/video_proxy.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db_uri = f"sqlite://{db_path}"

        _db_instance = DAL(
            db_uri,
            folder=os.getenv("DB_FOLDER", "/app/data/databases"),
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            migrate=False,
        )

        logger.info(f"Database connected: {db_type}")

    return _db_instance


def init_database() -> DAL:
    """Initialize database tables using PyDAL."""
    db = get_db()

    # Stream configurations table
    db.define_table(
        "stream_configurations",
        Field("id", "id"),
        Field("community_id", "string", length=255, notnull=True, unique=True),
        Field("stream_key", "string", length=255, notnull=True, unique=True),
        Field("stream_key_hash", "string", length=255, notnull=True),
        Field("is_active", "boolean", default=True, notnull=True),
        Field("max_bitrate", "integer", default=6000),
        Field("max_resolution", "string", length=50, default="1080p"),
        Field("created_at", "datetime", default=lambda: __import__("datetime").datetime.utcnow()),
        Field("updated_at", "datetime", update=lambda: __import__("datetime").datetime.utcnow()),
    )

    # Stream destinations table
    db.define_table(
        "stream_destinations",
        Field("id", "id"),
        Field("config_id", "reference stream_configurations", notnull=True, ondelete="CASCADE"),
        Field("platform", "string", length=50, notnull=True),
        Field("rtmp_url", "string", length=512, notnull=True),
        Field("stream_key", "string", length=255, notnull=True),
        Field("resolution", "string", length=50, default="1080p"),
        Field("bitrate", "integer", default=6000),
        Field("is_enabled", "boolean", default=True, notnull=True),
        Field("force_cut", "boolean", default=False, notnull=True),
        Field("created_at", "datetime", default=lambda: __import__("datetime").datetime.utcnow()),
        Field("updated_at", "datetime", update=lambda: __import__("datetime").datetime.utcnow()),
    )

    # Stream sessions table
    db.define_table(
        "stream_sessions",
        Field("id", "id"),
        Field("config_id", "reference stream_configurations", notnull=True, ondelete="CASCADE"),
        Field("session_id", "string", length=255, notnull=True, unique=True),
        Field("started_at", "datetime", default=lambda: __import__("datetime").datetime.utcnow()),
        Field("ended_at", "datetime"),
        Field("bytes_received", "bigint", default=0),
        Field("bytes_sent", "bigint", default=0),
        Field("is_active", "boolean", default=True, notnull=True),
    )

    # Stream analytics table
    db.define_table(
        "stream_analytics",
        Field("id", "id"),
        Field("session_id", "reference stream_sessions", notnull=True, ondelete="CASCADE"),
        Field("destination_id", "reference stream_destinations", notnull=True, ondelete="CASCADE"),
        Field("timestamp", "datetime", default=lambda: __import__("datetime").datetime.utcnow()),
        Field("bitrate", "integer"),
        Field("fps", "integer"),
        Field("dropped_frames", "integer", default=0),
        Field("errors", "integer", default=0),
    )

    db.commit()
    logger.info("Database tables initialized")

    return db
