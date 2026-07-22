"""Learner profile: placement-test results + ongoing feedback, used to calibrate
which words/phrases are worth capturing for THIS user (not a generic guess).

Stored in data/profile.json:
  {
    "level": "advanced",            # derived smart_level the loop should use
    "min_band": 6,                  # lowest difficulty band still worth capturing
    "known": ["alleged", ...],      # things the user marked as already known
    "unknown": ["ragtag", ...],     # things the user marked as not understood
    "history": [...]                # timestamped marks for auditing
  }
"""
import json
import os

import config

PROFILE_PATH = os.path.join(config.DATA, "profile.json")
LEVEL_BANK = os.path.join(config.DATA, "level_bank.json")

# band -> smart_level mapping (where the user's knowledge tops out)
_BAND_TO_LEVEL = {1: "intermediate", 2: "intermediate", 3: "intermediate",
                  4: "advanced", 5: "advanced", 6: "advanced",
                  7: "expert", 8: "expert"}


def load_profile():
    default = {"level": None, "min_band": None, "known": [], "unknown": [], "history": []}
    if os.path.exists(PROFILE_PATH):
        try:
            with open(PROFILE_PATH, encoding="utf-8") as handle:
                default.update(json.load(handle))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_profile(profile):
    os.makedirs(config.DATA, exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, ensure_ascii=False)


def load_bank():
    with open(LEVEL_BANK, encoding="utf-8") as handle:
        return json.load(handle)


def score_test(unknown_words, now_iso):
    """Given the words the user marked as NOT known, compute their level.

    Logic: find the lowest band where they miss >= 2 of 6 items — that's where
    their reliable knowledge ends. The band just below becomes their floor, and
    we capture at that band and above. Maps to a smart_level for the LLM.
    """
    bank = load_bank()
    unknown = {w.lower() for w in unknown_words}

    first_weak_band = None
    band_results = []
    for band in bank["bands"]:
        items = band["items"]
        missed = sum(1 for it in items if it.lower() in unknown)
        band_results.append({"band": band["band"], "cefr": band["cefr"],
                             "missed": missed, "total": len(items)})
        if missed >= 2 and first_weak_band is None:
            first_weak_band = band["band"]

    # capture at the first band they struggle with (and above); if they know
    # everything, target the very top band.
    min_band = first_weak_band if first_weak_band else bank["bands"][-1]["band"]
    level = _BAND_TO_LEVEL.get(min_band, "advanced")

    profile = load_profile()
    profile["level"] = level
    profile["min_band"] = min_band
    for w in unknown_words:
        if w not in profile["unknown"]:
            profile["unknown"].append(w)
    profile["history"].append({"at": now_iso, "type": "placement",
                               "unknown": sorted(unknown), "level": level,
                               "min_band": min_band})
    save_profile(profile)

    # also push the derived level into the capture config
    cfg = config.load_config()
    cfg["smart_level"] = level
    config.save_config(cfg)

    return {"level": level, "min_band": min_band, "bands": band_results}


_LEVELS = ["beginner", "intermediate", "advanced", "expert"]


def mark(term, known, now_iso):
    """Record ongoing feedback and ADAPT the capture level automatically.

    Every ✓ (knew it) / ✗ (new) nudges a running signal. When recent marks lean
    consistently one way, the level shifts so future captures match better:
      • too many ✓ in a row  -> captures are too EASY -> raise level (stricter)
      • too many ✗ in a row  -> captures are too HARD/just-right; if user is
        drowning in unknowns we could lower, but ✗ is the goal, so we only react
        to a strong run of "too easy".
    """
    profile = load_profile()
    bucket = "known" if known else "unknown"
    other = "unknown" if known else "known"
    if term not in profile[bucket]:
        profile[bucket].append(term)
    if term in profile[other]:
        profile[other].remove(term)
    profile["history"].append({"at": now_iso, "type": "mark",
                               "term": term, "known": known})

    # adaptive level: look at the last 15 marks; only adjust with a strong, sustained
    # signal (widened from 8 to stop the level thrashing intermediate<->advanced from
    # a handful of marks). Requires >=12 marks before it will move at all.
    recent = [h for h in profile["history"] if h.get("type") == "mark"][-15:]
    if len(recent) >= 12:
        known_ratio = sum(1 for h in recent if h.get("known")) / len(recent)
        cur = profile.get("level") or "advanced"
        idx = _LEVELS.index(cur) if cur in _LEVELS else 1
        new_idx = idx
        if known_ratio >= 0.75 and idx < len(_LEVELS) - 1:
            new_idx = idx + 1          # mostly "too easy" -> stricter
        elif known_ratio <= 0.10 and idx > 0:
            new_idx = idx - 1          # almost nothing known -> easier
        if new_idx != idx:
            profile["level"] = _LEVELS[new_idx]
            cfg = config.load_config()
            cfg["smart_level"] = _LEVELS[new_idx]
            config.save_config(cfg)
            profile["history"].append({"at": now_iso, "type": "auto-adjust",
                                       "from": _LEVELS[idx], "to": _LEVELS[new_idx],
                                       "known_ratio": round(known_ratio, 2)})

    save_profile(profile)
    return profile


def calibration_hint():
    """A short natural-language hint about the user's level + known/unknown samples,
    injected into the capture LLM prompt so it targets the right difficulty."""
    p = load_profile()
    if not p.get("level"):
        return ""
    known = ", ".join(p["known"][-25:]) or "(none yet)"
    unknown = ", ".join(p["unknown"][-25:]) or "(none yet)"
    return (
        f"\nLearner calibration — their tested level is {p['level']}.\n"
        f"They ALREADY KNOW these (never flag words this easy): {known}.\n"
        f"They did NOT know these (this is the difficulty to target): {unknown}.\n"
    )
