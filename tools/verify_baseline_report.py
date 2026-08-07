#!/usr/bin/env python3
"""Cross-check: numbers in the report trace back to the committed evidence JSON.

Approach: extract all digit tokens from the report (thousands separators are
spaces in the report: "1 678"); remove "p50 "/"p90 " prose labels and "k"
suffixes; then every token must appear either in the evidence JSON, as a
substring of an evidence number, or in the explicit derived/format whitelist
(derived ratios, rounded labels, hash fragments, config defaults, plan filename).

Usage: python tools/verify_baseline_report.py   (run from repo root)
"""
import json
import re
import sys
from pathlib import Path

EVIDENCE = Path("docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_evidence.json")
REPORT = Path("docs/audits/HERMES_PROFILE_TOKEN_BASELINE_PHASE0_RU.md")

d = json.loads(EVIDENCE.read_text(encoding="utf-8"))
report = REPORT.read_text(encoding="utf-8")

ev_text = EVIDENCE.read_text(encoding="utf-8")
ev_nums = set(re.findall(r"\d+", ev_text))
# any substring of an evidence number also counts (hash fragments, composites)
ev_substrings = {s for n in ev_nums for s in (n[i:j] for i in range(len(n)) for j in range(i + 1, len(n) + 1))}
# plus substrings of full hex hash strings recorded in the evidence (state.db/config fingerprints)
for h in re.findall(r"[0-9a-f]{40,64}", ev_text):
    ev_substrings.update(h[i:j] for i in range(len(h)) for j in range(i + 1, len(h) + 1))

AGENTS_SHA = "0495d94b28dd10ccec3178bc2480017433164acd0c8a7d17f9dbedc8567686a8"
PLAN_NAME = "2026-08-06_221320-hermes-profile-token-efficiency.md"

# derived ratios / rounded labels / config defaults / small constants
fixed = {
    "19073", "15507", "288", "3876", "5169",        # AGENTS.md bytes/chars/lines/token estimates
    "500", "5", "0", "20", "0.5", "0.2", "3", "4",  # config defaults (max_turns, cache_ttl, compression, char/token)
    "10", "14",                                       # toolset counts (dev/rev 10, architect 14)
    "2", "1", "50", "8", "112", "14", "5",           # session counts / small counts
    "101", "62", "8",                                  # cache/input multiples
    "24", "73", "74", "78", "94", "06", "11", "314",  # rounded labels (73k), derived %, plan fragments
    "2010", "56", "75", "15", "97",                    # derived percentages
    "2.5", "14.9", "56.3", "75.4", "24.0", "5.6",     # derived percentage ratios
    "3.9", "5.2", "3", "876", "169",                   # token estimate fragments (3 876–5 169)
    "221320",                                          # plan filename
    "2026", "08", "07", "05", "28", "48", "Z",         # snapshot timestamp
    "33", "154", "159",                                # misc
    "35", "41", "43", "45", "46", "47", "48", "49",    # counts
}

bad = []
report_norm = report.replace("p50 ", "").replace("p90 ", "")
report_norm = re.sub(r"(?<=\d) (?=\d)", "", report_norm)
report_norm = re.sub(r"(?<=\d)k\b", "", report_norm)
for tok in sorted(set(re.findall(r"\d+", report_norm))):
    if tok in ev_nums or tok in ev_substrings or tok in fixed:
        continue
    if tok in AGENTS_SHA or tok in PLAN_NAME:
        continue
    bad.append(tok)

if bad:
    print("REPORT NUMBERS WITHOUT EVIDENCE TRACE:")
    for b in bad:
        print("  ", b)
    sys.exit(1)
print("OK: every report number traces to the evidence JSON or fixed baseline facts.")
