#!/usr/bin/env python3
"""Regression checks for final blocking-finding accounting."""
from v31_finalize_quality import active_final_blockers


verified = [{"issue_id": "resolved"}, {"issue_id": "open"}]
lifecycle = [{"issue_id": "resolved", "status": "resolved_repair"}]
smoke = [{"issue_id": "smoke"}]
assert active_final_blockers(verified, lifecycle, smoke) == [
    {"issue_id": "open"}, {"issue_id": "smoke"},
]
print("Pact v3.1 final quality self-tests passed")
