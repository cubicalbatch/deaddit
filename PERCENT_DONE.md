# PERCENT_DONE
- Percent: ~84%
- Last completed: 6A — guarded media serving and public serialization (commit 94f3091)
- In flight: 6B — responsive image cards and detail images
- Next: 6C expand/minimize JS, 7A moderation lifecycle, Phase 8 verification
- Media routes: GET /media/images/original/<filename>, GET /media/images/thumbnail/<filename>
  (Cache-Control: public, max-age=300; 404 for unknown/traversed/missing/soft-removed)
- Public image payload: original_url, thumbnail_url, mime_type, width, height, alt_text
- Paid fal generations used so far: 0 of 10 budget.
