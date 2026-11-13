from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    data: bytes = b""

    def digest(self) -> str:
        return sha256(f"{self.kind}:{self.mode}:".encode() + self.data).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\x00" in value:
        raise ValueError(f"Expected a workspace-relative path: {value!r}")
    return path.as_posix()


def within(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(prefix == "." or path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def snapshot(root: Path) -> dict[str, Entry]:
    entries = {}
    remaining = 8 * 1024 * 1024
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                if len(entries) >= 512:
                    raise ValueError("Snapshot supports up to 512 workspace entries")
                path = Path(child.path)
                key = path.relative_to(root).as_posix()
                info = child.stat(follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISDIR(info.st_mode):
                    entries[key] = Entry("directory", mode)
                    pending.append(path)
                elif stat.S_ISLNK(info.st_mode):
                    entries[key] = Entry("symlink", mode, os.fsencode(os.readlink(path)))
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1 or info.st_size > remaining:
                        raise ValueError(f"Hard link or snapshot size limit at {key}")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as handle:
                        data = handle.read(remaining + 1)
                    remaining -= len(data)
                    if remaining < 0:
                        raise ValueError("Snapshot exceeds 8 MiB")
                    entries[key] = Entry("file", mode, data)
                else:
                    raise ValueError(f"Unsupported workspace entry: {key}")
    return entries


def restore(root: Path, before: dict[str, Entry], after: dict[str, Entry], changed: list[str]) -> bool:
    # The sandbox has stopped, and the workspace is owned by this experiment.
    order = sorted(changed, key=lambda path: (path.count("/"), path))
    for key in reversed(order):
        old, new = before.get(key), after.get(key)
        if new is not None and (old is None or old.kind != new.kind or new.kind == "symlink"):
            path = root / key
            if new.kind == "directory":
                path.rmdir()
            else:
                path.unlink()
    for key in order:
        old = before.get(key)
        if old is None:
            continue
        path = root / key
        if old.kind == "directory":
            path.mkdir(exist_ok=True)
        elif old.kind == "symlink":
            path.symlink_to(os.fsdecode(old.data))
        else:
            with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(old.data)
                    handle.close()
                    temporary.chmod(old.mode)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
    for key in reversed(order):
        if key in before and before[key].kind == "directory":
            (root / key).chmod(before[key].mode)
    return snapshot(root) == before
