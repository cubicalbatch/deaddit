# PERCENT_DONE
- Percent: ~53%
- Last completed: 4A — image-aware atomic post creation (commit 468746c)
- In flight: 4B — register and authorize create_image_post
- Next: 4C prompts, then Phase 5 (vision-aware reading)
- 4A service API for 4B: preflight_image_post(user, subdeaddit, title) then
  create_image_post(title, content, user, subdeaddit, image=PendingPostImage(...)).
  Content service owns DB only; 4B owns filesystem rollback.
- Paid fal generations used so far: 0 of 10 budget.
