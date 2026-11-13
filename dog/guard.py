from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Protocol
from uuid import uuid4

from .evidence import relative_path, restore, snapshot, within
from .policy import CommandPolicy
from .runner import Execution


class Runner(Protocol):
    def run(self, command: str, *, timeout: float) -> Execution: ...


@dataclass(frozen=True)
class Outcome:
    decision: str
    reason: str
    output: str
    returncode: int
    event: dict


class Guard:
    def __init__(self, workspace: Path, runner: Runner, policy: CommandPolicy, audit: Path,
                 *, allowed_writes=(), protected=("wrangler.toml", ".git")):
        self.workspace = workspace.resolve(strict=True)
        self.runner = runner
        self.policy = policy
        self.audit = audit.resolve()
        if self.audit.is_relative_to(self.workspace):
            raise ValueError("Audit log must be outside the agent workspace")
        self.allowed_writes = tuple("." if path == "." else relative_path(path) for path in allowed_writes)
        self.protected = tuple(map(relative_path, protected))
        self.halted = False

    def _log(self, event: dict) -> None:
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        with self.audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")

    def run(self, command: str, *, declared_writes=None, claimed_unchanged=(), timeout=10) -> Outcome:
        start = time.perf_counter_ns()
        event = {
            "run_id": uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command, "cwd": "/workspace", "tier": self.policy.tier,
            "allowed_writes": self.allowed_writes, "protected": self.protected,
            "executed": False, "simulated": False, "changed_paths": [],
            "rollback_attempted": False, "rollback_success": None, "execution_ms": 0.0,
        }

        def finish(decision: str, reason: str, output: str = "", code: int = 77) -> Outcome:
            event.update(phase="finished", decision=decision, reason=reason, returncode=code)
            event["guard_ms"] = max(0, (time.perf_counter_ns() - start) / 1e6 - event["execution_ms"])
            self._log(event)
            return Outcome(decision, reason, output, code, event)

        if self.halted:
            return finish("ERROR", "Workspace requires inspection after a previous failure")
        verdict = self.policy.inspect(command)
        event["simulated"] = verdict.simulated
        if verdict.decision != "ALLOW":
            return finish(verdict.decision, verdict.reason)
        if verdict.simulated:
            return finish("ALLOW", verdict.reason, verdict.reason, 0)
        try:
            declared = None if declared_writes is None else tuple(map(relative_path, declared_writes))
            unchanged = tuple(map(relative_path, claimed_unchanged))
            event.update(declared_writes=declared, claimed_unchanged=unchanged)
            if declared and any(not within(path, self.allowed_writes) or within(path, self.protected) for path in declared):
                return finish("BLOCK", "Declared writes exceed trusted scope")
            before = snapshot(self.workspace)
            self._log({**event, "phase": "before_execution"})
            execution_start = time.perf_counter_ns()
            event["executed"] = True
            try:
                result = self.runner.run(command, timeout=timeout)
            finally:
                event["execution_ms"] = (time.perf_counter_ns() - execution_start) / 1e6
            after = snapshot(self.workspace)
            changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
            violations = [path for path in changed if (
                within(path, self.protected) or not within(path, self.allowed_writes)
                or (declared is not None and not within(path, declared)) or within(path, unchanged)
            )]
            output_bytes = result.output.encode("utf-8")
            event.update(changed_paths=changed, violations=violations, command_returncode=result.returncode,
                         output_sha256=sha256(output_bytes).hexdigest(), output_bytes=len(output_bytes))
            event["evidence"] = {
                path: {"before": before[path].digest() if path in before else None,
                       "after": after[path].digest() if path in after else None}
                for path in changed
            }
            if violations or (changed and result.interrupted):
                event["rollback_attempted"] = True
                event["rollback_success"] = restore(self.workspace, before, after, changed)
                if not event["rollback_success"]:
                    raise RuntimeError("Rollback did not restore the snapshot")
            if result.interrupted:
                return finish("ERROR", result.output, code=result.returncode)
            if violations:
                return finish("POST_VIOLATION", "Changed outside scope or contradicted a claim: " + ", ".join(violations))
            return finish("ALLOW", verdict.reason, result.output, result.returncode)
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            self.halted = event["executed"]
            if event["rollback_attempted"]:
                event["rollback_success"] = False
            return finish("ERROR", str(error), code=70)
