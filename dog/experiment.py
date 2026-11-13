from importlib.resources import files
import json
import math
from pathlib import Path
import platform
import statistics
import sys
from tempfile import TemporaryDirectory
import time

from .guard import Guard
from .policy import CommandPolicy
from .runner import DockerRunner, docker_info


FIXTURES = {
    "app.py": "def total(quantity, unit_price):\n    return quantity + unit_price\n",
    "notes.txt": "DOG fixture\n",
    "wrangler.toml": 'name = "dog-fixture"\n',
}
VERIFY_TOTAL = (
    "python -c 'from app import total; "
    "assert total(2, 3) == 6; assert total(0, 9) == 0; assert total(4, 7) == 28'"
)


def seed_workspace(root: Path) -> None:
    for name, content in FIXTURES.items():
        (root / name).write_text(content, encoding="utf-8")


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def run_benchmark(output: Path, image: str, repeat: int, *, analysis_only: bool = False) -> dict:
    scenarios = json.loads(files("dog").joinpath("scenarios.json").read_text(encoding="utf-8"))
    policy = CommandPolicy()
    runtime = {} if analysis_only else docker_info(image)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for trial in range(1, repeat + 1):
        for scenario in scenarios:
            if scenario["mode"] == "analysis":
                start = time.perf_counter_ns()
                verdict = policy.inspect(scenario["command"])
                event = {"command": scenario["command"], "decision": verdict.decision, "reason": verdict.reason,
                         "executed": False, "simulated": verdict.simulated,
                         "guard_ms": (time.perf_counter_ns() - start) / 1e6,
                         "rollback_attempted": False, "rollback_success": None}
            elif analysis_only:
                continue
            else:
                with TemporaryDirectory(prefix="dog-case-", dir="/tmp") as directory:
                    root = Path(directory)
                    seed_workspace(root)
                    guard = Guard(root, DockerRunner(root, runtime["image_id"]), policy, output / "audit.jsonl",
                                  allowed_writes=scenario.get("allowed_writes", ()))
                    event = guard.run(scenario["command"], declared_writes=scenario.get("declared_writes"),
                                      claimed_unchanged=scenario.get("claimed_unchanged", ())).event
            rows.append({"scenario": scenario["id"], "label": scenario["label"], "mode": scenario["mode"],
                         "trial": trial, **event})
    report = {"environment": {"python": sys.version.split()[0], "host": platform.platform(), **runtime},
              "repeat": repeat, "policy": {"tier": policy.tier, "budget_units": policy.budget_units},
              "overall": summarize(rows),
              "by_mode": {mode: summarize([row for row in rows if row["mode"] == mode])
                          for mode in sorted({row["mode"] for row in rows})},
              "cases": rows}
    write_json(output / "results.json", report)
    return report


def summarize(rows: list[dict]) -> dict:
    counts = dict.fromkeys(("tp", "fp", "tn", "fn", "errors"), 0)
    for row in rows:
        if row["decision"] == "ERROR":
            counts["errors"] += 1
            continue
        detected = row["decision"] in {"BLOCK", "BUDGET_BLOCK", "POST_VIOLATION"}
        actual = row["label"] == "destructive"
        counts["tp" if actual and detected else "fn" if actual else "fp" if detected else "tn"] += 1
    tp, fp, tn, fn, _ = counts.values()
    rollbacks = [row for row in rows if row["rollback_attempted"]]
    restored = sum(row["rollback_success"] is True for row in rollbacks)

    def ratio(a, b):
        return a / b if b else None

    def latency(values):
        values = sorted(values)
        return {"n": len(values), "median": statistics.median(values),
                "p95": values[math.ceil(0.95 * len(values)) - 1]} if values else None

    executed = [row for row in rows if row["executed"] and row["decision"] != "ERROR"]
    return {"cases": len(rows), **counts, "detection_rate": ratio(tp, tp + fn),
            "false_negative_rate": ratio(fn, tp + fn), "false_positive_rate": ratio(fp, fp + tn),
            "rollback_attempted": len(rollbacks), "rollback_restored": restored,
            "rollback_success_rate": ratio(restored, len(rollbacks)),
            "guard_ms": latency(row["guard_ms"] for row in rows if row["decision"] != "ERROR"),
            "guard_executed_ms": latency(row["guard_ms"] for row in executed),
            "docker_execution_ms": latency(row["execution_ms"] for row in executed),
            "allowed_command_failures": sum(row["decision"] == "ALLOW" and row.get("returncode", 0) != 0 for row in rows)}
