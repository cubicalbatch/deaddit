"""Gunicorn configuration for Deaddit application."""


# Server socket
bind = "0.0.0.0:5000"
backlog = 2048

# Worker processes
workers = 2
worker_class = "sync"
worker_connections = 1000
timeout = 120
keepalive = 5

# Restart workers after this many requests, to help control memory usage
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = "deaddit"

# Security
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# SSL (if needed)
# keyfile = "/path/to/keyfile"
# certfile = "/path/to/certfile"

# Preload app for better memory usage and faster worker startup. Safe since A5:
# create_app() no longer starts any scheduler or background thread at import
# time (threads would not survive fork — historical hazard documented in
# refactor/architecture.md). Background work now lives in the dedicated
# `deaddit-worker` process instead.
preload_app = True

# Graceful timeout for worker restart
graceful_timeout = 30


def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info("Worker spawned (pid: %s)", worker.pid)


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    pass


def when_ready(server):
    """Called just after the server is started."""
    server.log.info("Server is ready. Spawning workers")


def worker_int(worker):
    """Called when a worker receives the SIGINT signal."""
    worker.log.info("Worker received SIGINT signal")


def pre_exec(server):
    """Called just before a new master process is forked."""
    server.log.info("Forked child, re-executing")


def post_worker_init(worker):
    """Called when a worker is initialized."""
    worker.log.info("Worker initialized (pid: %s)", worker.pid)
