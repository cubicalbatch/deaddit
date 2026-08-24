"""Shared Flask extension instances.

These are created unbound (no app argument) and attached to the application
in :func:`deaddit.create_app` via ``init_app``, so importing this module
performs no I/O and requires no application context.
"""

from flask_caching import Cache
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
cache = Cache()
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
