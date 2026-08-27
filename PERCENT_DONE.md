# PERCENT_DONE
- Percent: ~88%
- Last completed: 6B — responsive image cards and detail rendering (commit 57cb0fd)
- In flight: 6C — per-image and feed-wide expand/minimize behavior
- Next: 7A moderation/cleanup lifecycle, then Phase 8 verification (8A browser subagent, 8B, 8C)
- Toolbar lives in .feed-wrap as a sibling BEFORE .feed-page, so hx-select=".feed-page" never duplicates it.
  Visibility is CSS-only: .feed-wrap:has(.post-card__media) .image-feed-toolbar { display: flex }
- 6B intentionally left ALL per-card expand controls to 6C.
- Paid fal generations used so far: 0 of 10 budget.
