#!/usr/bin/env python3
"""Regression checks for v3.1 final terminal policy."""
from v31_final_lifecycle import terminal_status


def status(prior_status: str | None) -> str:
    return terminal_status(
        ledger_ok=True,
        coverage_ok=True,
        verification_ok=True,
        smoke_ok=True,
        blocking_findings=[],
        final_repair_rounds=1,
        prior_status=prior_status,
    )


assert status(None) == "complete"
assert status("failed") == "complete"
assert status("quarantined") == "quarantined"
assert terminal_status(
    ledger_ok=False,
    coverage_ok=True,
    verification_ok=True,
    smoke_ok=True,
    blocking_findings=[],
    final_repair_rounds=1,
    prior_status="failed",
) == "failed"
print("Pact v3.1 final lifecycle self-tests passed")
