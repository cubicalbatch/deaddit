"""LLM accounting ledger: one LLMUsage row per provider attempt.

AttemptRecorder is wired as the transport's ``on_attempt`` sink by
deaddit.llm.client. The ledger must NEVER break generation: every DB
failure logs a warning and skips the row (commit-per-row; SQLite WAL,
low volume — batching deferred).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import time
from urllib.parse import urlparse

from deaddit.extensions import db
from deaddit.models import LLMUsage, ModelPrice

logger = logging.getLogger(__name__)


def _is_local_endpoint(api_url: str) -> bool:
    """True for localhost / loopback / RFC1918 private endpoints."""
    host = urlparse(api_url).hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _match_price(model: str) -> ModelPrice | None:
    """Longest glob pattern (case-insensitive) wins; None when unpriced."""
    best: ModelPrice | None = None
    for price in ModelPrice.query.all():
        if not fnmatch.fnmatch(model.lower(), price.pattern.lower()):
            continue
        if best is None or len(price.pattern) > len(best.pattern):
            best = price
    return best


def estimate_cost(
    api_url: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    """USD estimate for a response, or None when the price is unknown.

    Local endpoints cost exactly 0.0. Hosted endpoints need both a
    matching ModelPrice and at least one known token count; otherwise
    the result is None (rendered downstream as unknown, never fake $0).
    """
    if _is_local_endpoint(api_url):
        return 0.0
    price = _match_price(model)
    if price is None:
        return None
    if prompt_tokens is None and completion_tokens is None:
        return None
    return (prompt_tokens or 0) * price.prompt_price_per_1k + (
        completion_tokens or 0
    ) * price.completion_price_per_1k


class AttemptRecorder:
    """Collects one LLMUsage row per provider attempt for a single request."""

    def __init__(self, req) -> None:
        self.req = req
        self._invoked = False
        self._rows_recorded = 0
        self._last_mark = time.monotonic()

    def mark_invoked(self) -> None:
        """Call right before the provider invocation."""
        self._invoked = True

    # ------------------------------------------------------------------
    # transport callback

    def on_attempt(self, attempt: int, scoped_id: str, outcome) -> None:
        """Called by the transport once per retry-loop iteration.

        ``outcome`` is the parsed response dict on success or the raised /
        last Exception of that iteration.
        """
        latency_ms = (time.monotonic() - self._last_mark) * 1000.0
        self._last_mark = time.monotonic()

        if isinstance(outcome, BaseException):
            row = self._base_row(attempt, latency_ms)
            row.status = "failed"
            row.error_type = type(outcome).__name__
        else:
            usage = (outcome or {}).get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            row = self._base_row(attempt, latency_ms)
            row.status = "ok"
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.total_tokens = usage.get("total_tokens")
            row.estimated_cost = estimate_cost(
                self.req.api_url,
                self.req.model,
                prompt_tokens,
                completion_tokens,
            )
        self._commit(row)

    # ------------------------------------------------------------------
    # finalization

    def finalize(self, exc: BaseException | None = None, data: dict | None = None):
        """Record a backfill row iff the provider ran but recorded nothing.

        Fakes and non-cooperating providers never invoke ``on_attempt``;
        backfill ONE row from the parsed response (ok) or from the escaping
        exception (failed, attempt=1). If the provider was never invoked
        (e.g. pre-flight CapabilityError), record NOTHING.
        """
        if not self._invoked or self._rows_recorded > 0:
            return
        if exc is not None:
            row = self._base_row(1, None)
            row.status = "failed"
            row.error_type = type(exc).__name__
        else:
            usage = (data or {}).get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            row = self._base_row(1, None)
            row.status = "ok"
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.total_tokens = usage.get("total_tokens")
            row.estimated_cost = estimate_cost(
                self.req.api_url,
                self.req.model,
                prompt_tokens,
                completion_tokens,
            )
        return self._commit(row)

    # ------------------------------------------------------------------

    def _base_row(self, attempt: int, latency_ms: float | None) -> LLMUsage:
        req = self.req
        return LLMUsage(
            request_id=req.request_id,
            attempt=attempt,
            api_url=req.api_url,
            model=req.model,
            action=getattr(req, "action", None),
            agent=getattr(req, "agent", None),
            latency_ms=latency_ms,
        )

    def _commit(self, row: LLMUsage) -> LLMUsage | None:
        """Persist one row; NEVER raise — a ledger failure must not break
        generation."""
        try:
            db.session.add(row)
            db.session.commit()
        except Exception:
            logger.warning(
                "LLM ledger write failed (request_id=%s attempt=%s); "
                "skipping row",
                row.request_id,
                row.attempt,
                exc_info=True,
            )
            try:
                db.session.rollback()
            except Exception:
                pass
            return None
        self._rows_recorded += 1
        return row
