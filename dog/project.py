"""Install a project-local DOG integration for Codex."""

import argparse
from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .evidence import relative_path, snapshot
from .policy import TIERS
from .runner import docker_info


INSTRUCTIONS = (
    "Use the DOG MCP run_command tool to read, edit, and test this repository. "
    "It runs Bash from this Git checkout's root, mounted as /workspace. Use relative paths. "
    "For edits, use a suitable command through DOG. Respect the configured writable paths "
    "and explain any blocked operation to the user."
)
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "hooks", "computer_use", "browser_use",
    "multi_agent", "worktrees",
)


def install_project(repo: Path, image: str, tier: str, allowed: list[str] | None,
                    protected: list[str], audit: Path | None) -> tuple[Path, Path]:
    if find_spec("mcp") is None:
        raise RuntimeError("Install DOG with the [codex] extra first")
    if sys.version_info < (3, 11):
        raise RuntimeError("The Codex installer requires Python 3.11 or newer")
    import tomllib

    repo = Path(subprocess.run(
        ["git", "-C", str(repo.resolve(strict=True)), "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")).resolve(strict=True)
    config = repo / ".codex" / "config.toml"
    if config.parent.is_symlink():
        raise ValueError(f"Refusing to install through a symlink: {config.parent}")
    if config.exists() or config.is_symlink():
        raise FileExistsError(f"{config} already exists. Preserve it and merge the DOG settings manually.")

    writable = [path if path == "." else relative_path(path) for path in (allowed or ["."])]
    protected_paths = [".git", ".codex", *(relative_path(path) for path in protected)]
    if audit is None:
        state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        repo_id = sha256(os.fsencode(repo)).hexdigest()[:12]
        audit = state_dir / "dog" / f"{repo.name}-{repo_id}.jsonl"
    audit = audit.resolve()
    if audit.is_relative_to(repo):
        raise ValueError("The audit file must be outside the checkout")

    snapshot(repo)
    image_id = docker_info(image)["image_id"]
    args = ["-m", "dog.project", str(repo), image_id, str(audit), "--tier", tier]
    for path in writable:
        args.extend(["--allow", path])
    for path in protected_paths:
        args.extend(["--protect", path])

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    user_config = codex_home / "config.toml"
    user_servers = {}
    if user_config.is_file():
        user_servers = tomllib.loads(user_config.read_text(encoding="utf-8")).get("mcp_servers", {})
    lines = [
        'sandbox_mode = "read-only"',
        'approval_policy = "never"',
        'web_search = "disabled"',
        f"developer_instructions = {json.dumps(INSTRUCTIONS)}",
        "",
        "[features]",
        *(f"{feature} = false" for feature in DISABLED_FEATURES),
        "",
        "[mcp_servers.dog]",
        f"command = {json.dumps(sys.executable, ensure_ascii=False)}",
        f"cwd = {json.dumps(str(Path(__file__).resolve().parents[1]), ensure_ascii=False)}",
        f"args = {json.dumps(args, ensure_ascii=False)}",
        "enabled = true",
        "required = true",
        'default_tools_approval_mode = "approve"',
        "tool_timeout_sec = 50",
    ]
    for name in sorted(user_servers.keys() - {"dog"}):
        if not re.fullmatch(r"[\w-]+", name, flags=re.ASCII):
            raise ValueError(f"Unsupported MCP server name in user config: {name}")
        lines.extend(["", f"[mcp_servers.{name}]", "enabled = false"])
    config.parent.mkdir(exist_ok=True)
    with config.open("x", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    return config, audit


def main() -> None:
    from .codex_demo import serve

    parser = argparse.ArgumentParser(description="Run the project-local DOG MCP server")
    parser.add_argument("repo", type=Path)
    parser.add_argument("image")
    parser.add_argument("audit", type=Path)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--protect", action="append", required=True)
    args = parser.parse_args()
    serve(args.repo, args.image, args.audit, "with-dog", "", "",
          project_writes=tuple(args.allow), project_protected=tuple(args.protect),
          tier=args.tier, instructions=INSTRUCTIONS)


if __name__ == "__main__":
    main()
