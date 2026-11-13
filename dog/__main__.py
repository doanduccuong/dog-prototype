import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from .experiment import run_benchmark
from .policy import CommandPolicy, TIERS
from .runner import DEFAULT_IMAGE


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m dog")
    sub = parser.add_subparsers(dest="operation", required=True)
    check = sub.add_parser("check", help="Analyze a Bash command without executing it")
    check.add_argument("--tier", choices=TIERS, default="staging")
    check.add_argument("command", help="Pass the full Bash command as one quoted argument")
    install = sub.add_parser("install", help="Configure DOG for Codex in a Git checkout")
    install.add_argument("repo", type=Path, nargs="?", default=Path.cwd())
    install.add_argument("--image", default=DEFAULT_IMAGE, help="Local Docker image with Bash and project tools")
    install.add_argument("--tier", choices=TIERS, default="development")
    install.add_argument("--allow", action="append", help="Writable path, repeatable (default: entire checkout)")
    install.add_argument("--protect", action="append", default=[], help="Additional protected path, repeatable")
    install.add_argument("--audit", type=Path, help="Audit file outside the checkout")
    benchmark = sub.add_parser("benchmark", help="Replay the fixed 50-case experiment")
    benchmark.add_argument("--repeat", type=positive_int, default=1)
    benchmark.add_argument("--analysis-only", action="store_true")
    agent = sub.add_parser("agent", help="Run mini-SWE-agent on the built-in fixture")
    agent.add_argument("--model", help="Provider/model name. Omit to use scripted responses")
    agent.add_argument("--step-limit", type=positive_int, default=15)
    codex_demo = sub.add_parser("codex-demo", help="Compare Codex and incident replay with and without DOG")
    codex_demo.add_argument("--model", help="Use this Codex model in both arms")
    codex_demo.add_argument("--replay-only", action="store_true", help="Replay the incident without invoking Codex")
    repo_demo = sub.add_parser("repo-demo", help="Run the Codex comparison on itsdangerous issue 126")
    repo_demo.add_argument("repo", type=Path, help="Local clone of pallets/itsdangerous with the historical commits")
    repo_demo.add_argument("--model", help="Use this Codex model in both arms")
    repo_mode = repo_demo.add_mutually_exclusive_group()
    repo_mode.add_argument("--agent-case-only", action="store_true",
                           help="Run the controlled repository-instruction experiment")
    repo_mode.add_argument("--observe", type=positive_int, metavar="TRIALS",
                           help="Observe unprompted scope violations and replay the first one with DOG")
    for command in (benchmark, agent, codex_demo, repo_demo):
        command.add_argument("--image", default=DEFAULT_IMAGE, help="A trusted image already present in Docker")
        command.add_argument("--output", type=Path, help="A new directory for run artifacts")
    args = parser.parse_args()
    if args.operation == "install":
        from .project import install_project

        try:
            config, audit = install_project(args.repo, args.image, args.tier, args.allow, args.protect, args.audit)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            parser.exit(1, f"Could not install DOG: {getattr(error, 'stderr', None) or error}\n")
        print(f"DOG installed: {config}\nAudit: {audit}\nOpen the checkout and start codex.")
        return 0
    if args.operation == "check":
        result = CommandPolicy(args.tier).inspect(args.command)
        print(json.dumps(asdict(result), indent=2))
        return 0 if result.decision == "ALLOW" else 77
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or Path("artifacts") / f"{args.operation}-{timestamp}").resolve()
    try:
        if args.operation == "benchmark":
            report = run_benchmark(output, args.image, args.repeat, analysis_only=args.analysis_only)
            result = report["overall"]
            code = int(bool(result["errors"] or result["allowed_command_failures"]))
        elif args.operation == "agent":
            from .mini import run_agent

            result = run_agent(output, args.image, model_name=args.model, step_limit=args.step_limit)
            code = int(not result["passed"])
        elif args.operation == "codex-demo":
            from .codex_demo import run_demo

            result = run_demo(output, args.image, model=args.model, replay_only=args.replay_only)
            code = int(not result["passed"])
        else:
            from .repo_demo import run_observation, run_repo_demo

            repo = args.repo.resolve(strict=True)
            if args.observe is not None:
                result = run_observation(repo, output, args.image, trials=args.observe, model=args.model)
                code = int(result["status"] not in {"no_violation_observed", "violation_replayed"})
            else:
                result = run_repo_demo(repo, output, args.image, model=args.model,
                                       agent_case_only=args.agent_case_only)
                code = int(not result["passed"])
    except subprocess.CalledProcessError as error:
        print((error.stderr or str(error)).strip(), file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        extra = "codex" if args.operation in {"codex-demo", "repo-demo"} else "agent"
        print(f"{error}. Install with: python -m pip install '.[{extra}]'", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print(f"Artifacts: {output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
