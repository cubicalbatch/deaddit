# PERCENT_DONE
- Percent: ~95%
- Last completed: 7A — media lifecycle across destructive paths (commit 93cfab2). Phase 7 done.
- In flight: 8A — independent browser verification subagent
- Next: 8B deterministic regression sweep, 8C bounded end-to-end smoke (paid fal step needs user approval)
- Full test suite green as of 93cfab2.
- 7A found and fixed a real bug: user/subdeaddit bulk deletes used query-level Post deletes that
  bypassed ORM cascades, orphaning PostImage rows and files.
- CLI: deaddit images reconcile-media (dry-run default; --apply; --i-know-this-is-prod guard)
- Paid fal generations used so far: 0 of 10 budget.
