"""
Socket.io event handlers: the /admin namespace connection lifecycle and the
public /live activity ticker (pump: deaddit/runtime/live_pump.py).
"""

import functools
import logging
from datetime import datetime

from flask_socketio import disconnect, emit, join_room, leave_room

from deaddit.extensions import socketio

logger = logging.getLogger(__name__)


def handle_socket_errors(f):
    """Decorator to handle socket errors gracefully."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Socket error in {f.__name__}: {str(e)}")
            try:
                emit("error", {"message": "Connection error occurred"})
            except Exception:
                # If emit fails, disconnect the client
                disconnect()

    return wrapper


@socketio.on("connect", namespace="/admin")
@handle_socket_errors
def admin_connect(*args):
    """Handle admin WebSocket connection."""
    logger.info("Admin client connected to WebSocket")
    emit("connected", {"status": "Connected to admin WebSocket"})


@socketio.on("disconnect", namespace="/admin")
def admin_disconnect(*args):
    """Handle admin WebSocket disconnection."""
    logger.info("Admin client disconnected from WebSocket")


@socketio.on("connect_error", namespace="/admin")
def admin_connect_error(data):
    """Handle admin WebSocket connection errors."""
    logger.error(f"Admin WebSocket connection error: {data}")


@socketio.on("ping", namespace="/admin")
@handle_socket_errors
def handle_ping():
    """Handle ping for connection testing."""
    emit("pong", {"timestamp": datetime.utcnow().isoformat()})


@socketio.on_error(namespace="/admin")
def admin_error_handler(e):
    """Handle WebSocket errors in admin namespace."""
    logger.error(f"WebSocket error in admin namespace: {str(e)}")
    try:
        emit("error", {"message": "An error occurred", "details": str(e)})
    except Exception:
        # If emit fails, just log and continue
        logger.error("Failed to emit error message to client")


# ---------------------------------------------------------------------------
# UX-6: public live-activity ticker. Namespace "/live" is PUBLIC (no auth,
# unlike the admin namespace); the emit pump lives in
# deaddit/runtime/live_pump.py. Contiguous block -- later phases append below.
# ---------------------------------------------------------------------------


@socketio.on("join_activity", namespace="/live")
@handle_socket_errors
def join_activity(data=None):
    """Join the public activity room and start the live-count pump."""
    from deaddit.runtime.live_pump import ROOM as ACTIVITY_ROOM
    from deaddit.runtime.live_pump import get_live_pump

    join_room(ACTIVITY_ROOM)
    logger.info("Client joined activity room")
    # note_join initialises the watermark to the current max event ts and
    # starts the pump thread (idempotent).
    get_live_pump().note_join(ACTIVITY_ROOM)
    emit("joined", {"room": ACTIVITY_ROOM})


@socketio.on("leave_activity", namespace="/live")
@handle_socket_errors
def leave_activity(data=None):
    """Leave the public activity room."""
    from deaddit.runtime.live_pump import ROOM as ACTIVITY_ROOM
    from deaddit.runtime.live_pump import get_live_pump

    leave_room(ACTIVITY_ROOM)
    logger.info("Client left activity room")
    get_live_pump().note_leave(ACTIVITY_ROOM)
    emit("left", {"room": ACTIVITY_ROOM})


@socketio.on("activity_loaded", namespace="/live")
@handle_socket_errors
def activity_loaded(data):
    """Client ack after an explicit load: reset pending, advance watermark."""
    from deaddit.runtime.live_pump import get_live_pump

    payload = data if isinstance(data, dict) else {}
    get_live_pump().note_activity_loaded(payload.get("ts"))
