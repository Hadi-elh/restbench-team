"""Reusable agent runner — plays a full game via the RestBench HTTP API.

Usage:
    from agents.runner import run_game
    from agents.naive_rule import strategy

    result = run_game(strategy, base_url="http://localhost:8001", team_name="naive", seed=42)
    print(result)

A strategy is a callable: (observation: dict, day: int) -> list[dict]
Each dict in the list is a tool call: {"tool": "place_order", "args": {...}}
"""

from __future__ import annotations

import os
import time
from typing import Callable

import httpx

Strategy = Callable[[dict, int], list[dict]]

DEFAULT_URL = os.getenv("RESTBENCH_URL", "http://localhost:8001")

# Exponential backoff delays between retry attempts (seconds).
# Total attempts = len(_RETRY_DELAYS) + 1 = 4 (initial + 3 retries).
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_MAX_RETRIES = len(_RETRY_DELAYS)


def _is_retryable(exc: Exception) -> bool:
    """True iff the exception is transient and worth retrying."""
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


def _post_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict | None = None,
) -> httpx.Response:
    """POST with up to _MAX_RETRIES retries on transient errors.

    Raises immediately on non-retryable errors (4xx, programming errors).
    Re-raises the last retryable exception once all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return r
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                print(
                    f"  [retry {attempt + 1}/{_MAX_RETRIES}] "
                    f"{type(exc).__name__} on {url} — sleeping {delay}s"
                )
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _diagnose_day(ss: dict, prev_staff: int, weather: str) -> list[str]:
    """Return a list of plain-English factors that shaped this day's performance."""
    notes: list[str] = []

    covers = int(ss.get("total_covers", 0) or 0)
    unavailable = ss.get("dishes_unavailable_at", {}) or {}
    walkout_band = ss.get("walkout_band", "None")
    avg_wait = float(ss.get("avg_wait_minutes", 0) or 0)
    pk_wait = float(ss.get("peak_wait_minutes", 0) or 0)

    if unavailable:
        so_list = sorted(unavailable.items(), key=lambda x: x[1])
        so_str = ", ".join(f"{d} ran out @{h}:00" for d, h in so_list)
        notes.append(f"STOCKOUTS → {so_str}")

    if walkout_band in ("Some", "Many"):
        notes.append(
            f"CAPACITY PRESSURE → walkouts={walkout_band}, "
            f"avg wait {avg_wait:.0f}min / peak {pk_wait:.0f}min"
            f" (consider more staff or smaller menu)"
        )

    if weather == "stormy":
        notes.append("weather=stormy → significantly fewer walk-ins")
    elif weather == "rainy":
        notes.append("weather=rainy → somewhat fewer walk-ins")
    elif weather == "sunny":
        notes.append("weather=sunny → demand boost")

    if covers == 0 and not unavailable:
        notes.append("ZERO covers with no recorded stockouts — likely full stockout or zero demand")
    elif covers < 20 and not unavailable and walkout_band == "None":
        notes.append("low demand, no capacity/stockout signal — may be slow weekday or weather effect")

    return notes


def _print_day_debug(
    day_num: int,
    day_of_week: str,
    weather: str,
    prev_staff: int,
    dr: dict,
    new_obs: dict,
    accepted: int,
    rejected: int,
) -> None:
    ss = new_obs.get("service_summary") or {}
    covers = dr["total_covers"]
    revenue = float(dr["total_revenue"])
    cash = float(new_obs.get("cash", 0))
    rep = new_obs.get("reputation_band", "?")

    print(
        f"  Day {day_num} ({day_of_week}): covers={covers}, "
        f"revenue={revenue:.0f}, cash={cash:.0f}, "
        f"actions={accepted}ok/{rejected}rej"
    )

    # Staff / weather / rep context
    print(f"    staff={prev_staff} | weather={weather} | rep={rep}")

    # Dishes sold (top 3 by count)
    dishes_sold = ss.get("dishes_sold", {}) or {}
    if dishes_sold:
        top3 = sorted(dishes_sold.items(), key=lambda x: -x[1])[:3]
        sold_str = ", ".join(f"{d}×{n}" for d, n in top3)
        print(f"    sold: {sold_str}")
    else:
        print("    sold: (none)")

    # Walkout + wait time summary
    wb = ss.get("walkout_band", "None")
    avg_w = float(ss.get("avg_wait_minutes", 0) or 0)
    pk_w = float(ss.get("peak_wait_minutes", 0) or 0)
    print(f"    walkouts={wb} | wait avg={avg_w:.1f}min peak={pk_w:.1f}min")

    # Diagnosis
    factors = _diagnose_day(ss, prev_staff, weather)
    for f in factors:
        print(f"    ⚑ {f}")


def run_game(
    strategy: Strategy,
    *,
    base_url: str = DEFAULT_URL,
    team_name: str = "agent",
    scenario: str = "baseline",
    seed: int = 42,
    verbose: bool = True,
) -> dict:
    transport = httpx.HTTPTransport(retries=3)
    with httpx.Client(base_url=base_url, timeout=60.0, transport=transport) as client:
        # Game creation — unchanged; low-level retries handled by transport.
        r = client.post("/games", json={
            "team_name": team_name,
            "scenario": scenario,
            "seed": seed,
        })
        r.raise_for_status()
        data = r.json()
        game_id = data["game_id"]
        observation = data["observation"]
        day = data["day"]

        if verbose:
            print(f"Game {game_id} created — Day {day}, Cash: {observation['cash']}")

        for turn in range(30):
            # Capture context BEFORE end-turn (these reflect the day that is about to run).
            prev_day_of_week: str = observation.get("day_of_week", "")
            prev_weather: str = observation.get("weather_today", "")
            prev_staff: int = int(observation.get("staff_level", 0) or 0)

            tool_calls = strategy(observation, day)

            accepted = 0
            rejected = 0
            for tc in tool_calls:
                # Track staff changes so the debug line shows the ACTUAL staff for the day.
                if tc.get("tool") == "set_staff_level":
                    try:
                        prev_staff = int(tc["args"]["level"])
                    except (KeyError, TypeError, ValueError):
                        pass
                try:
                    r = _post_with_retry(client, f"/games/{game_id}/action", tc)
                    result = r.json()
                    if result["status"] == "accepted":
                        accepted += 1
                    else:
                        rejected += 1
                        if verbose:
                            print(
                                f"  Day {day}: REJECTED {tc['tool']}: "
                                f"{result.get('reason')}"
                            )
                except Exception as exc:
                    # All retries exhausted for this action — count as rejected and
                    # continue to the next action rather than aborting the turn.
                    if verbose:
                        print(
                            f"  WARNING: Day {day} action '{tc['tool']}' failed after "
                            f"{_MAX_RETRIES} retries ({type(exc).__name__}), "
                            f"counting as rejected"
                        )
                    rejected += 1

            # end-turn failure is fatal: we can't advance the day without it.
            try:
                r = _post_with_retry(client, f"/games/{game_id}/end-turn")
            except Exception as exc:
                print(
                    f"  FATAL: end-turn for game {game_id} day {day} failed after "
                    f"all retries: {type(exc).__name__}: {exc}"
                )
                raise

            turn_data = r.json()
            observation = turn_data["observation"]
            day = turn_data["day"]
            status = turn_data["status"]
            dr = turn_data["day_result"]

            if verbose:
                _print_day_debug(
                    day_num=day - 1,
                    day_of_week=prev_day_of_week,
                    weather=prev_weather,
                    prev_staff=prev_staff,
                    dr=dr,
                    new_obs=observation,
                    accepted=accepted,
                    rejected=rejected,
                )

            if status != "in_progress":
                if verbose:
                    print(f"Game ended: {status}")
                break

        r = client.get(f"/games/{game_id}/score")
        r.raise_for_status()
        score_data = r.json()

        if verbose:
            s = score_data["score"]
            print(f"\nFinal score: {s['total_score']}")
            print(f"  Net profit: {s['net_profit']}")
            print(f"  Satisfaction penalty: {s['satisfaction_penalty']}")
            print(f"  Reputation penalty: {s['reputation_penalty']}")
            print(f"  Walkout penalty: {s['walkout_penalty']}")
            print(f"  Waste penalty: {s['waste_penalty']}")
            print(f"  Days survived: {score_data['days_survived']}")
            print(f"  Final cash: {score_data['final_cash']}")

        return score_data


if __name__ == "__main__":
    print("Use: python -m agents.do_nothing / agents.naive_rule / agents.starter_template")
