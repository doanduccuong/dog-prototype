from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import subprocess
import time
from uuid import uuid4


DEFAULT_IMAGE = "python:3.12-slim-bookworm"
OUTPUT_LIMIT = 64 * 1024


@dataclass(frozen=True)
class Execution:
    output: str
    returncode: int
    interrupted: bool = False


def docker_info(image: str) -> dict:
    engine = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    info = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}} {{.Os}}/{{.Architecture}}"],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    return {"docker_engine": engine, "image": image, "image_id": info.split()[0], "platform": info.split()[1]}


class DockerRunner:
    def __init__(self, workspace: Path, image: str = DEFAULT_IMAGE):
        self.workspace = workspace.resolve(strict=True)
        self.image = image
        if "," in str(self.workspace) or "\n" in str(self.workspace) or os.getuid() == 0:
            raise ValueError("Use an unprivileged host user and a workspace path without commas or newlines")

    def run(self, command: str, *, timeout: float = 10) -> Execution:
        if not 0 < timeout <= 60:
            raise ValueError("Timeout must be greater than zero and at most 60 seconds")
        name = f"dog-trial-{uuid4().hex}"
        args = [
            "docker", "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--pids-limit=64", "--memory=256m", "--cpus=1",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,src={self.workspace},dst=/workspace",
            "--workdir", "/workspace", "--entrypoint", "/bin/bash", self.image,
            "--noprofile", "--norc", "-c", 'ulimit -f 8192; exec /bin/bash --noprofile --norc -c "$1"',
            "dog-runner", command,
        ]
        output = bytearray()
        deadline = time.monotonic() + timeout
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise TimeoutError("Command timed out")
                        chunk = os.read(process.stdout.fileno(), 8192)
                        if not chunk:
                            break
                        if len(output) + len(chunk) > OUTPUT_LIMIT:
                            raise TimeoutError("Command exceeded its output limit")
                        output.extend(chunk)
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except BaseException as error:
                process.kill()
                process.wait()
                self._stop_container(name)
                if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                    return Execution(str(error), 124, interrupted=True)
                raise
        decoded = output.decode("utf-8", errors="replace")
        if code == 125:
            raise RuntimeError(f"Docker could not run the command: {decoded.strip()}")
        return Execution(decoded, code)

    @staticmethod
    def _stop_container(name: str) -> None:
        result = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=15)
        if result.returncode and "No such container" not in result.stderr:
            raise RuntimeError(f"Unable to confirm cleanup of {name}: {result.stderr.strip()}")
