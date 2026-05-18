"""Main agent entry point — rule layers (ordering + operations + pricing + marketing) + LLM.

Action assembly order (matters for predictability, not engine correctness):
    1. ordering_actions        — Amr's rule-based supply reorders
    2. ops_actions             — Nadim's rule-based staffing / happy hour / etc.
    3. rule_price_actions      — Hadi's rule-based pricing (multiplicative stack)
    4. rule_marketing_actions  — Hadi's rule-based marketing spend
    5. rule_special_actions    — Hadi's rule-based daily special selection
    6. safe_llm                — whatever the LLM returned, filtered to allowed tools
    7. save_notes              — persisted memory, always last
"""

from __future__ import annotations

try:
    from agents.ordering import get_ordering_actions
except ImportError:
    def get_ordering_actions(obs, day, notes_state=None):
        return [], {}

try:
    from agents.operations import get_operations_actions
except ImportError:
    def get_operations_actions(obs, day, notes_state):
        return [], {}

from agents.memory import (
    parse_notes,
    build_notes,
    compute_revenue_trend,
    compute_consumption,
    update_scenario_flags,
    build_day_entry,
    build_stockout_entries,
    get_best_daily_special,
    get_price_actions,
    get_marketing_actions,
    detect_drift_flags,
)
from agents.state_compressor import compress_observation
from agents.llm_layer import get_llm_actions
from agents.runner import run_game


def strategy(observation: dict, day: int) -> list[dict]:
    # 1. Parse persisted notes memory.
    notes_state = parse_notes(observation.get("notes", ""))

    # 2. Rule modules first (they own ordering + operations decisions).
    ordering_actions, ordering_state = get_ordering_actions(observation, day, notes_state)
    ops_actions_raw, ops_state = get_operations_actions(observation, day, notes_state)
    # Strip offer_daily_special from ops — rule_special_actions (step 5) owns it exclusively
    # to avoid emitting two identical tool calls per turn.
    ops_actions = [a for a in ops_actions_raw if a.get("tool") != "offer_daily_special"]

    # 3. Merge module-owned state into notes_state at the TOP LEVEL (not nested).
    # This is the bug-3 fix: previously the ops state dict was being stuffed
    # under notes_state["scenario_flags"], nesting the real flags one level too
    # deep and breaking sticky-flag reads on the next turn.
    for k, v in (ops_state or {}).items():
        notes_state[k] = v
    for k, v in (ordering_state or {}).items():
        notes_state[k] = v

    # 4. Rule-based pricing (multiplicative stack — fires whenever prices need moving).
    rule_price_actions = get_price_actions(
        observation, notes_state.get("scenario_flags", {})
    )

    # 4b. Drift detection and rule-based marketing spend.
    drift_flags = detect_drift_flags(notes_state.get("day_history", []), notes_state)
    notes_state["drift_flags"] = drift_flags
    rule_marketing_actions = get_marketing_actions(
        observation,
        notes_state.get("scenario_flags", {}),
        notes_state.get("revenue_trend", "stable"),
    )

    # 5. Rule-based daily special — always picks the best estimated-revenue dish.
    rule_special_actions: list[dict] = []
    special_dish = get_best_daily_special(observation)
    if special_dish:
        rule_special_actions.append(
            {"tool": "offer_daily_special", "args": {"dish": special_dish}}
        )

    # 6. Compress observation for LLM context.
    compressed = compress_observation(observation, day, notes_state)

    # 7. LLM handles happy hour advisory. set_price only when multiplier stack didn't fire.
    llm_actions = get_llm_actions(compressed, observation, day)

    # Tools the LLM may emit. Rules own ordering, staffing, marketing, daily special.
    llm_allowed = {"run_happy_hour", "save_notes", "set_price"}
    if rule_price_actions:
        # Multiplicative pricing fired today — LLM should not also touch prices.
        llm_allowed.discard("set_price")
    safe_llm = [a for a in llm_actions if a.get("tool") in llm_allowed]

    # 8. Update notes memory and append save_notes as the LAST action.
    updated_state = update_notes_state(notes_state, observation, day)
    save_action = {"tool": "save_notes", "args": {"text": build_notes(updated_state)}}

    return (
        ordering_actions
        + ops_actions
        + rule_price_actions
        + rule_marketing_actions
        + rule_special_actions
        + safe_llm
        + [save_action]
    )


def update_notes_state(notes_state: dict, observation: dict, day: int) -> dict:
    """Build the next-turn notes state using Hadi's helpers.

    Keeps day_history and stockout_log to the last 5 entries each so the
    serialized notes stay well under the 4000-char cap.
    """
    history = notes_state.get("day_history", [])
    history.append(build_day_entry(observation, day))
    notes_state["day_history"] = history[-5:]

    stockouts = notes_state.get("stockout_log", [])
    stockouts.extend(build_stockout_entries(observation, day))
    notes_state["stockout_log"] = stockouts[-5:]

    notes_state["revenue_trend"] = compute_revenue_trend(notes_state["day_history"])

    rc = notes_state.get("recent_consumption", [])
    rc.append({"day": day, "consumption": compute_consumption(observation)})
    notes_state["recent_consumption"] = rc[-5:]

    notes_state["scenario_flags"] = update_scenario_flags(
        notes_state.get("scenario_flags", {}),
        observation.get("alerts", []) or [],
    )
    return notes_state


if __name__ == "__main__":
    result = run_game(strategy, team_name="SpicyMeatballs", seed=42)
