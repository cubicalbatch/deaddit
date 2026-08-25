"""Dedicated background-worker runtime for Deaddit.

This package hosts the standalone worker process (`deaddit-worker`) that owns
ALL background job execution: atomic claiming, heartbeats, crash recovery, and
the recurring (nightly) maintenance registrations. The web process never
schedules jobs itself.
"""
