"""Recompute the PR#33 primary criterion after excluding compaction-STORM sessions.

Inputs are the PUBLISHED figures from _dashboard/pr33-repro-hunt.md (Part 2 global
cells + the robustness section). Nothing is re-derived from raw turns here; this is a
transparent sensitivity analysis on the hunt's own numbers, so it inherits their
rounding (rates are quoted to 4 dp).

Exclusion criterion, fixed BEFORE looking at the effect on the ratio and applied
identically to both arms: a session whose `context:compaction` events are >=90%
at strategy_level 8 (max) with after_tokens above target is a compaction STORM --
a different defect from the standing-stale-notice PR #33 addresses. Measured
against the graph, both arms' top outliers are 100% level-8 storms.
"""

# --- published global cells (later-notice turns) ---
SON_TURNS, SON_RATE = 11_810, 0.2451
OPU_TURNS, OPU_RATE = 22_267, 0.1226

# --- published outliers (both verified as 100%-level-8 storms) ---
SON_STORM_TURNS, SON_STORM_DUPS = 972, 2_188      # 445ac89c... anchors-amp-dev-architect
OPU_STORM_TURNS, OPU_STORM_DUPS = 2_163, 2_248    # 877774a5... foundation-explorer

son_dups = SON_TURNS * SON_RATE
opu_dups = OPU_TURNS * OPU_RATE


def rate(d, t):
    return d / t if t else 0.0


rows = [
    ("as published (no exclusion)",
     rate(son_dups, SON_TURNS), rate(opu_dups, OPU_TURNS)),
    ("exclude sonnet storm only",
     rate(son_dups - SON_STORM_DUPS, SON_TURNS - SON_STORM_TURNS),
     rate(opu_dups, OPU_TURNS)),
    ("exclude opus storm only",
     rate(son_dups, SON_TURNS),
     rate(opu_dups - OPU_STORM_DUPS, OPU_TURNS - OPU_STORM_TURNS)),
    ("exclude BOTH storms (the principled cut)",
     rate(son_dups - SON_STORM_DUPS, SON_TURNS - SON_STORM_TURNS),
     rate(opu_dups - OPU_STORM_DUPS, OPU_TURNS - OPU_STORM_TURNS)),
]

print(f"sonnet-5 dups implied by published cell: {son_dups:,.0f}")
print(f"opus-5   dups implied by published cell: {opu_dups:,.0f}")
print(f"sonnet storm share of sonnet dups: {SON_STORM_DUPS / son_dups:.1%} "
      f"(hunt says 76%)")
print(f"opus   storm share of opus dups:   {OPU_STORM_DUPS / opu_dups:.1%} "
      f"(hunt says 83%)")
print()
print(f"{'cut':<42} {'sonnet':>8} {'opus':>8} {'ratio':>8}   verdict")
print("-" * 82)
for label, s, o in rows:
    r = s / o if o else float("inf")
    if r >= 2.0:
        v = "REPRODUCED"
    elif r < 1.3:
        v = "NOT-REPRODUCED"
    else:
        v = "INCONCLUSIVE"
    print(f"{label:<42} {s:>8.4f} {o:>8.4f} {r:>7.2f}x   {v}")
print()
print("pre-registered rule: >=2.0x REPRODUCED, <1.3x NOT-REPRODUCED, else INCONCLUSIVE")
print("hunt's own leave-top-3-sessions-out variant: 0.0488 / 0.0187 = 2.61x")
