"""Offline experiment planner. Never launches models, containers, or network calls."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any


def number(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return float(value)


def count(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def approximate_paired_sample_size(gain: float, discordance: float, alpha: float, power: float) -> int:
    if not 0 < gain < 1 or not gain <= discordance <= 1:
        raise ValueError("Sample-size inputs require 0 < gain < 1 and gain <= discordance <= 1")
    if not 0 < alpha < 1 or not 0.5 < power < 1:
        raise ValueError("Require 0 < alpha < 1 and 0.5 < power < 1")
    normal = NormalDist()
    z_sum = normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)
    return math.ceil(z_sum**2 * (discordance - gain**2) / gain**2)


def estimate(
    plan: dict,
    requested: list[str] | None = None,
    include_all: bool = False,
    unit_cost: float | None = None,
    minutes: float | None = None,
    workers: int | None = None,
) -> dict:
    if plan.get("status") != "planning_only" or plan.get("schema_version") != 1:
        raise ValueError("Expected a schema_version=1, planning_only configuration")
    parallel = count(workers if workers is not None else plan["parallel_workers"], "parallel_workers", 1)
    duration = number(minutes if minutes is not None else plan["minutes_per_run"], "minutes_per_run")
    utilization = number(plan["utilization"], "utilization")
    if not 0 < utilization <= 1:
        raise ValueError("utilization must be in (0, 1]")
    reserve = number(plan["reserve_fraction"], "reserve_fraction")
    phases = plan["phases"]
    phase_ids = [p["id"] for p in phases]
    if len(set(phase_ids)) != len(phase_ids):
        raise ValueError("Phase IDs must be unique")
    if requested:
        unknown = set(requested) - set(phase_ids)
        if unknown:
            raise ValueError(f"Unknown phases: {', '.join(sorted(unknown))}")
    selected = set(requested or [])
    summaries = []
    for phase in phases:
        included = include_all or (phase["id"] in selected if requested else phase["enabled_by_default"])
        if not included:
            continue
        factor_names = ("cases", "arms", "models", "repeats")
        factors = {name: count(phase[name], f"{phase['id']}.{name}", 1) for name in factor_names}
        extra = count(phase["extra_runs"], f"{phase['id']}.extra_runs")
        base_runs = math.prod(factors.values())
        summaries.append(
            {
                "id": phase["id"],
                "name": phase["name"],
                **factors,
                "factorial_runs": base_runs,
                "extra_runs_upper_bound": extra,
                "planned_runs_upper_bound": base_runs + extra,
            }
        )
    if not summaries:
        raise ValueError("No phases selected")
    total = sum(p["planned_runs_upper_bound"] for p in summaries)
    cost_inputs = plan["cost_per_run_scenarios"] if unit_cost is None else [
        {"name": "user_supplied_unit_cost", "usd": unit_cost}
    ]
    costs = []
    for scenario in cost_inputs:
        price = number(scenario["usd"], "cost_per_run_usd")
        costs.append(
            {
                "name": scenario["name"],
                "assumed_usd_per_run": price,
                "estimated_model_cost_usd": round(total * price, 2),
                "with_reserve_usd": round(total * price * (1 + reserve), 2),
            }
        )
    sample_sizes = []
    alpha = number(plan["sample_size_alpha"], "sample_size_alpha")
    power = number(plan["sample_size_power"], "sample_size_power")
    for scenario in plan["sample_size_scenarios"]:
        gain = number(scenario["absolute_gain"], "absolute_gain")
        discordance = number(scenario["discordant_probability"], "discordant_probability")
        sample_sizes.append(
            {
                "assumed_absolute_gain": gain,
                "assumed_discordant_probability": discordance,
                "approximate_independent_pairs": approximate_paired_sample_size(gain, discordance, alpha, power),
            }
        )
    serial_hours = total * duration / 60
    return {
        "status": "planning_estimate_not_benchmark_result",
        "launches_model_jobs": False,
        "assumptions_notice": plan["assumptions_notice"],
        "currency": "USD",
        "phases": summaries,
        "planned_runs_upper_bound": total,
        "assumed_minutes_per_run": duration,
        "assumed_parallel_workers": parallel,
        "assumed_utilization": utilization,
        "serial_run_hours": round(serial_hours, 2),
        "approximate_elapsed_run_hours": round(serial_hours / parallel / utilization, 2),
        "reserve_fraction": reserve,
        "cost_scenarios": costs,
        "sample_size_alpha": alpha,
        "sample_size_power": power,
        "sample_size_rough_scenarios": sample_sizes,
        "sample_size_limitations": [
            "Normal approximation for independent paired binary cases",
            "Not a calculation for repeated seeds, correlated repositories, or non-inferiority",
            "Not an assertion that the proposed improvement exists",
        ],
        "excluded_costs": plan["exclusions"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("resources/preparation/experiment-plan.json"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--phase", action="append", help="Select a phase; repeat to select multiple phases")
    selection.add_argument("--all", action="store_true", help="Estimate every phase, including conditional phases")
    parser.add_argument("--unit-cost", type=float, help="Hypothetical or measured mean USD per complete model run")
    parser.add_argument("--minutes-per-run", type=float)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
        result = estimate(plan, args.phase, args.all, args.unit_cost, args.minutes_per_run, args.workers)
        serialized = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
