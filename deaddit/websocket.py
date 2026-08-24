"""
WebSocket handlers for real-time admin interface updates.
"""

import functools
from datetime import datetime

from flask_socketio import disconnect, emit, join_room, leave_room
import logging

logger = logging.getLogger(__name__)

from deaddit import socketio


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


@socketio.on("join_job_updates", namespace="/admin")
@handle_socket_errors
def join_job_updates(data):
    """Join job updates room for real-time job status."""
    join_room("job_updates")
    logger.info("Client joined job_updates room")
    emit("joined", {"room": "job_updates"})


@socketio.on("leave_job_updates", namespace="/admin")
@handle_socket_errors
def leave_job_updates(data):
    """Leave job updates room."""
    leave_room("job_updates")
    logger.info("Client left job_updates room")
    emit("left", {"room": "job_updates"})


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
