from dataclasses import dataclass
import re

import bashlex
from bashlex.errors import ParsingError


TIERS = ("development", "staging", "production")
READ_COMMANDS = {"ls", "cat", "head", "tail", "grep", "pwd", "wc", "echo", "printf", "true", "false", "find"}
STAGING_COMMANDS = READ_COMMANDS | {"cd", "python", "python3", "touch", "mkdir", "sed", "cp", "mv"}
BLOCKED = {
    "rm", "rmdir", "shred", "mkfs", "dd", "sudo", "su", "curl", "wget", "bash", "sh", "zsh",
    "dash", "fish", "eval", "source", ".", "exec", "env", "command", "builtin", "xargs",
    "git", "docker", "kubectl", "az", "gcloud", "psql", "mysql", "sqlite3", "trap", "enable",
}
SQL_DESTRUCTIVE = re.compile(r"\b(?:DROP\s+(?:TABLE|DATABASE)|DELETE\s+FROM|TRUNCATE)\b", re.I)
SUPPORTED_NODES = {"list", "pipeline", "command", "word", "operator", "pipe", "redirect", "heredoc"}
FIND_LITERAL = re.compile(r"""(?:[a-zA-Z0-9_./:+%=-]+|!|\\[()]|'[^'\n]*'|"[^"$\\\x60\n]*")""")
FIND_FILTERS = {"-name", "-iname", "-path", "-ipath", "-type", "-mindepth", "-maxdepth"}
FIND_FLAGS = {"-print", "-print0", "-empty", "-prune", "-xdev", "-a", "-and", "-o", "-or", "!", "-not", "(", ")"}


def find_is_read_only(command: str, arguments) -> bool:
    # Bash can expand globs and braces even when bashlex reports a plain word.
    # Require each argument to produce exactly the literal token we inspect.
    if any(word.parts or not FIND_LITERAL.fullmatch(command[slice(*word.pos)])
           for word in arguments):
        return False
    tokens = iter(word.word for word in arguments)
    in_expression = False
    for token in tokens:
        if token.startswith("-") or token in {"!", "(", ")", ","}:
            in_expression = True
        if not in_expression:
            continue
        if token in FIND_FILTERS:
            if next(tokens, None) is None:
                return False
        elif token not in FIND_FLAGS:
            return False
    return True


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str
    simulated: bool = False


@dataclass(frozen=True)
class CommandPolicy:
    tier: str = "staging"
    budget_units: int = 200

    def __post_init__(self):
        if self.tier not in TIERS or self.budget_units < 0:
            raise ValueError("Invalid policy tier or budget")

    def inspect(self, command: str) -> Verdict:
        if not command.strip() or len(command) > 16_384 or "\x00" in command:
            return Verdict("BLOCK", "Empty or oversized command, or command contains a NUL byte")
        try:
            trees = bashlex.parse(command)
        except (ParsingError, ValueError, NotImplementedError, RecursionError, IndexError, AttributeError):
            return Verdict("BLOCK", "Unsupported Bash syntax")
        if not trees:
            return Verdict("BLOCK", "No executable command")
        pending = list(reversed(trees))
        while pending:
            node = pending.pop()
            if node.kind not in SUPPORTED_NODES:
                return Verdict("BLOCK", f"Unsupported Bash construct: {node.kind}")
            if node.kind == "operator" and node.op == "&":
                return Verdict("BLOCK", "Background jobs are outside the supported syntax")
            if node.kind == "redirect":
                if self.tier == "production" and node.type not in {"<", "<<", "<<-"}:
                    return Verdict("BLOCK", "Production policy disallows write redirections")
                if getattr(node.output, "kind", None):
                    pending.append(node.output)
                # Limit heredocs to literal data because Bash may expand their contents.
                heredoc = getattr(node, "heredoc", None)
                if heredoc and ("$" in heredoc.value or "`" in heredoc.value):
                    return Verdict("BLOCK", "Potential expansion in a heredoc")
            if node.kind == "command":
                words = [part for part in node.parts if part.kind == "word"]
                if not words:
                    return Verdict("BLOCK", "Command name required")
                name, *args = (part.word for part in words)
                if not re.fullmatch(r"[a-zA-Z0-9_.+-]+", name):
                    return Verdict("BLOCK", "Executable must have a literal, bare command name")
                if name == "aws":
                    if len(trees) != 1 or node is not trees[0] or len(words) != len(node.parts):
                        return Verdict("BLOCK", "AWS simulation accepts a single plain command")
                    if any(part.parts for part in node.parts):
                        return Verdict("BLOCK", "AWS simulation requires literal arguments")
                    return self._budget(args)
                if name == "find" and not find_is_read_only(command, words[1:]):
                    return Verdict("BLOCK", "find requires literal paths and supported read-only expressions")
                if name in BLOCKED or any(SQL_DESTRUCTIVE.search(arg) for arg in args if name not in {"echo", "printf", "grep"}):
                    return Verdict("BLOCK", f"Destructive or out-of-scope operation: {name}")
                allowed = READ_COMMANDS if self.tier == "production" else STAGING_COMMANDS
                if self.tier != "development" and name not in allowed:
                    return Verdict("BLOCK", f"Command not allowlisted for {self.tier}: {name}")
            if node.kind in {"word", "command", "list", "pipeline"}:
                pending.extend(reversed(node.parts))
        return Verdict("ALLOW", "Command passed pre-execution policy")

    def _budget(self, args: list[str]) -> Verdict:
        rates = {"t3.micro": 10, "t2.large": 120, "m5.large": 100}
        if self.tier == "production" or args[:2] != ["ec2", "run-instances"] or len(args) != 6:
            return Verdict("BLOCK", "AWS operation outside the supported EC2 simulation")
        flags = dict(zip(args[2::2], args[3::2]))
        if set(flags) != {"--instance-type", "--count"}:
            return Verdict("BLOCK", "Use explicit --instance-type and --count in EC2 simulation")
        instance, count = flags["--instance-type"], flags["--count"]
        if instance not in rates or not re.fullmatch(r"[1-9][0-9]{0,5}", count):
            return Verdict("BLOCK", "Unknown instance type or invalid count")
        total = rates[instance] * int(count)
        decision = "BUDGET_BLOCK" if total > self.budget_units else "ALLOW"
        return Verdict(decision, f"EC2 simulation: {total} units; limit {self.budget_units}", simulated=True)
