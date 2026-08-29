"""Reply-chain fatigue helpers (thread realism).

Real reply chains between two people die after a couple of exchanges,
usually with the last reply left unanswered. These helpers derive, from
the comment chain alone (no extra state), how long two personas have
already been going back and forth at the tail of a thread, and the
deterministic per-pair exchange cap that ends the ping-pong.

Two consumers must stay in agreement:

- :func:`deaddit.dynamics.notifications.notify_comment_created`
  suppresses the reply notification once a reply *completes* the pair's
  exchange (tail >= cap), so the counterpart never returns just to
  answer it;
- :func:`deaddit.agents.tools_write._create_comment` rejects a reply
  that would *extend* the exchange past the cap (tail > cap).

Both compare against the same :func:`exchange_cap`, so the notification
side and the enforcement side can never disagree about when an exchange
is over. Third parties joining mid-chain start their own pair count, and
a fresh sub-thread under a different comment is a new chain — those
escape valves keep multi-party threads organic.
"""

from __future__ import annotations

import zlib

from deaddit.extensions import db
from deaddit.models import Comment, Setting

__all__ = ["exchange_cap", "exchange_tail_for_reply"]

#: Setting keys bounding the per-pair exchange cap. Defaults end most
#: two-person back-and-forths after 2-3 replies, matching how real
#: exchanges fizzle out.
_EXCHANGE_CAP_MIN = ("reply_exchange_cap_min", 2)
_EXCHANGE_CAP_MAX = ("reply_exchange_cap_max", 3)

#: Hard sanity ceiling on the configured cap, so a fat-fingered Setting
#: can never produce a rubber-room of endless sanctioned ping-pong.
_EXCHANGE_CAP_LIMIT = 10


def _cap_bound(key_and_default: tuple[str, int]) -> int:
    key, default = key_and_default
    raw = Setting.get_value(key, str(default))
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), _EXCHANGE_CAP_LIMIT)


def exchange_cap(post_id: int, author_a: str, author_b: str) -> int:
    """Deterministic exchange cap for one (unordered) pair inside one post.

    Stable across runs and processes without any stored state: the cap is
    a hash of ``(post_id, sorted pair)`` mapped into the configured
    ``[reply_exchange_cap_min, reply_exchange_cap_max]`` range. An
    inverted or degenerate range collapses to the minimum.
    """
    lo = _cap_bound(_EXCHANGE_CAP_MIN)
    hi = _cap_bound(_EXCHANGE_CAP_MAX)
    if hi <= lo:
        return lo
    pair = ":".join(sorted((author_a, author_b)))
    digest = zlib.crc32(f"{post_id}:{pair}".encode())
    return lo + (digest % (hi - lo + 1))


def _alternating_tail_length(authors_deepest_first: list[str]) -> int:
    """Length of the maximal two-author alternating run at the chain's end.

    ``authors_deepest_first[0]`` is the deepest (most recent) comment.
    The run must alternate between exactly two *distinct* authors; a
    self-reply collapses it to 1, and any third author (or a repeat that
    breaks the alternation) ends it.
    """
    if len(authors_deepest_first) < 2:
        return len(authors_deepest_first)
    first, second = authors_deepest_first[0], authors_deepest_first[1]
    if first == second:
        return 1
    length = 2
    while length < len(authors_deepest_first):
        expected = first if length % 2 == 0 else second
        if authors_deepest_first[length] != expected:
            break
        length += 1
    return length


def exchange_tail_for_reply(parent_id: int | None, reply_author: str) -> int:
    """Alternating-tail length the given reply lands in, including itself.

    ``parent_id`` is the comment being replied to and ``reply_author``
    the replying persona - the reply itself does not need to exist yet,
    which lets the agent tool check a *would-be* reply and the
    notification path check a freshly committed one through the same
    code.
    """
    authors = [reply_author]
    comment_id = parent_id
    visited: set[int] = set()
    while comment_id is not None and comment_id not in visited:
        visited.add(comment_id)
        row = db.session.get(Comment, comment_id)
        if row is None:
            break
        authors.append(row.user)
        comment_id = row.parent_id
    return _alternating_tail_length(authors)
