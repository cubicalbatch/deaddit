# PERCENT_DONE
- Percent: ~60%
- Last completed: 4B — register and authorize create_image_post (commit ace5f13)
- In flight: 4C — teach prompts when and how to use image posts
- Next: Phase 5 (5A vision capability, 5B image description, 5C read summaries)
- Gating: disabled -> create_post only; optional -> both; image_only -> create_image_post only.
  Enforced in registry (offering) and executor (_check_image_policy, independent of offering).
- Shared post budget: registry.POST_TOOL_NAMES drives per-run cap, hourly cap, duplicate checks.
- Paid fal generations used so far: 0 of 10 budget.
