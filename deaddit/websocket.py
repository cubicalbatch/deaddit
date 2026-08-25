"""
WebSocket handlers for real-time admin interface updates.
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


# ---------------------------------------------------------------------------
# UX-5: streamed job logs. Contiguous block -- later phases append below.
# ---------------------------------------------------------------------------


@socketio.on("join_job_log", namespace="/admin")
@handle_socket_errors
def join_job_log(data):
    """Join a specific job's live-log room and confirm readiness."""
    job_id = int((data or {}).get("job_id", 0))
    if not job_id:
        emit("error", {"message": "join_job_log requires a job_id"})
        return

    from deaddit.runtime.tailer import get_tailer

    join_room(f"job_log:{job_id}")
    # Lazy-start the DB->socket pump; it also repairs job_update emissions.
    get_tailer().note_join(job_id)
    logger.info("Client joined job_log room for job %s", job_id)
    emit("job_log_ready", {"job_id": job_id})


@socketio.on("leave_job_log", namespace="/admin")
@handle_socket_errors
def leave_job_log(data):
    """Leave a specific job's live-log room."""
    job_id = int((data or {}).get("job_id", 0))
    if not job_id:
        emit("error", {"message": "leave_job_log requires a job_id"})
        return

    from deaddit.runtime.tailer import get_tailer

    leave_room(f"job_log:{job_id}")
    get_tailer().note_leave(job_id)
    logger.info("Client left job_log room for job %s", job_id)
    emit("left", {"room": f"job_log:{job_id}"})


# ---------------------------------------------------------------------------
# LLM-4: live token streaming (watch-thoughts). Rooms are per request_id;
# events are emitted server-side by deaddit/llm/stream_admin.py as
# "llm_stream" {request_id, kind, data, ts} while a streamed generation runs.
# ---------------------------------------------------------------------------


@socketio.on("join_llm_stream", namespace="/admin")
@handle_socket_errors
def join_llm_stream(data):
    """Join the streaming room for one request_id before its POST starts."""
    request_id = str((data or {}).get("request_id", "")).strip()
    if not request_id:
        emit("error", {"message": "join_llm_stream requires a request_id"})
        return
    join_room(request_id)
    logger.info("Client joined llm_stream room %s", request_id)
    emit("llm_stream_ready", {"request_id": request_id})


@socketio.on("leave_llm_stream", namespace="/admin")
@handle_socket_errors
def leave_llm_stream(data):
    """Leave the streaming room for one request_id."""
    request_id = str((data or {}).get("request_id", "")).strip()
    if not request_id:
        emit("error", {"message": "leave_llm_stream requires a request_id"})
        return
    leave_room(request_id)
    logger.info("Client left llm_stream room %s", request_id)
    emit("left", {"room": request_id})


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
