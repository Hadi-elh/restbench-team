"""Main agent entry point — combines rule-based ordering/ops with LLM optimization."""

from __future__ import annotations

try:
    from agents.ordering import get_ordering_actions
except ImportError:
    def get_ordering_actions(obs, day): return []

try:
    from agents.operations import get_operations_actions
except ImportError:
    def get_operations_actions(obs, day, notes_state): return [], {}

try:
    from agents.memory import parse_notes, build_notes
except ImportError:
    import json as _json

    def parse_notes(s):
        try:
            return _json.loads(s) if s else {}
        except Exception:
            return {}

    def build_notes(d):
        try:
            return _json.dumps(d)[:4000]
        except Exception:
            return ""

from agents.state_compressor import compress_observation
from agents.llm_layer import get_llm_actions
from agents.runner import run_game


def strategy(observation: dict, day: int) -> list[dict]:
    # 1. Parse notes memory
    notes_state = parse_notes(observation.get("notes", ""))

    # 2. Get rule-based actions (ordering + operations)
    ordering_actions = get_ordering_actions(observation, day)
    ops_actions, scenario_flags = get_operations_actions(observation, day, notes_state)

    # 3. Merge scenario flags into notes_state
    notes_state["scenario_flags"] = scenario_flags

    # 4. Compress observation for LLM
    compressed = compress_observation(observation, day, notes_state)

    # 5. Get LLM optimization actions
    llm_actions = get_llm_actions(compressed, observation, day)

    # 6. Dedup: remove LLM actions that conflict with rule actions
    #    Rules win on: place_order, set_staff_level
    #    LLM wins on: set_price, set_marketing_spend, offer_daily_special, set_menu
    rule_tools = {a["tool"] for a in ordering_actions + ops_actions}
    safe_llm = [a for a in llm_actions if a["tool"] not in rule_tools]

    # 7. Update notes memory and append save_notes action
    updated_notes = build_notes(update_notes_state(notes_state, observation, day))
    if updated_notes:
        safe_llm.append({"tool": "save_notes", "args": {"text": updated_notes}})

    return ordering_actions + ops_actions + safe_llm


def update_notes_state(notes_state: dict, observation: dict, day: int) -> dict:
    ss = observation.get("service_summary", {}) or {}
    history = notes_state.get("day_history", [])
    history.append({
        "day": day,
        "revenue": observation.get("yesterday_revenue", 0),
        "cash": observation.get("cash", 0),
        "walkouts": ss.get("walkout_band", "None"),
    })
    notes_state["day_history"] = history[-5:]  # keep last 5

    stockouts = notes_state.get("stockout_log", [])
    for dish, hour in ss.get("dishes_unavailable_at", {}).items():
        stockouts.append({"day": day, "dish": dish, "hour": hour})
    notes_state["stockout_log"] = stockouts[-5:]

    return notes_state


if __name__ == "__main__":
    result = run_game(strategy, team_name="SpicyMeatballs", seed=42)
