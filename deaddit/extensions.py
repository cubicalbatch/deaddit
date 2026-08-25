"""Shared Flask extension instances.

These are created unbound (no app argument) and attached to the application
in :func:`deaddit.create_app` via ``init_app``, so importing this module
performs no I/O and requires no application context.
"""

from flask_caching import Cache
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
cache = Cache()
migrate = Migrate()
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
    allow_upgrades=False,
    transports=["polling"],
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Apply WAL / foreign-key / busy-timeout pragmas to sqlite connections.

    SQLAlchemy does not enable these by default; without them concurrent
    writers hit "database is locked" and foreign keys are silently ignored.
    The dialect check keeps non-sqlite engines (postgres etc.) untouched.
    """

    if dbapi_connection.__class__.__module__.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()
