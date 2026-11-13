import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dog.codex_demo import observe_command
from dog.evidence import snapshot
from dog.experiment import run_benchmark, seed_workspace
from dog.guard import Guard
from dog.policy import CommandPolicy, TIERS
from dog.repo_demo import PROTECTED_FILE, SOURCE_FILE, replay_observed_violation
from dog.runner import DockerRunner, Execution


class PolicyTests(unittest.TestCase):
    def test_commands_and_expansions(self):
        policy = CommandPolicy()
        for command in ("cat notes.txt | wc -l", "echo 'rm -rf /'", "echo hi > notes.txt",
                        "cat <<EOF\nliteral text\nEOF\n"):
            with self.subTest(command=command):
                self.assertEqual(policy.inspect(command).decision, "ALLOW")
        for command in ("cat notes.txt; rm -rf .", "echo $(rm -rf /)", "bash cleanup.sh", "echo >",
                        "sleep 1 & echo hi", "BASH_ENV=notes.txt echo hi",
                        "cat <<EOF\n$(rm -rf /)\nEOF\n", "echo x > >(bash)"):
            with self.subTest(command=command):
                self.assertEqual(policy.inspect(command).decision, "BLOCK")

    def test_tier_and_budget(self):
        self.assertEqual(CommandPolicy("production").inspect("echo hi > result.txt").decision, "BLOCK")
        self.assertEqual(CommandPolicy("staging").inspect("date").decision, "BLOCK")
        self.assertEqual(CommandPolicy("development").inspect("date").decision, "ALLOW")
        command = "aws ec2 run-instances --count {} --instance-type m5.large"
        self.assertTrue(CommandPolicy().inspect(command.format(2)).simulated)
        self.assertEqual(CommandPolicy().inspect(command.format(3)).decision, "BUDGET_BLOCK")
        self.assertEqual(CommandPolicy().inspect(command.format(2) + " && echo hi").decision, "BLOCK")

    def test_find_distinguishes_search_from_write_or_execution(self):
        read_only = (
            "find .. -name AGENTS.md -print",
            "find . -name '*.py'",
            r"find src -maxdepth 2 \( -name '*.py' -o -name '*.pyi' \) -type f -print0",
            "find . -name '-delete' -print",
        )
        rejected = (
            "find . -delete",
            r"find . -exec touch unwanted.txt \;",
            "find . -execdir touch unwanted.txt +",
            r"find . -ok touch unwanted.txt \;",
            "find . -fprint unwanted.txt",
            "find -files0-from inputs -print",
            "find . -name *",
            "find . -name foo{,-delete}",
            "find . -name \"$pattern\"",
            "find . -name",
            "find . -print && rm notes.txt",
        )
        for tier in TIERS:
            policy = CommandPolicy(tier)
            for command in read_only:
                with self.subTest(tier=tier, command=command):
                    self.assertEqual(policy.inspect(command).decision, "ALLOW")
            for command in rejected:
                with self.subTest(tier=tier, command=command):
                    self.assertEqual(policy.inspect(command).decision, "BLOCK")

    def test_analysis_benchmark(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "analysis"
            report = run_benchmark(output, "unused", 1, analysis_only=True)
            self.assertEqual(report["overall"]["cases"], 27)
            self.assertEqual(report["overall"]["tp"], 25)
            self.assertEqual(report, json.loads((output / "results.json").read_text()))


class FakeRunner:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def run(self, command, *, timeout=10):
        self.calls += 1
        return self.action()


class GuardTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "workspace"
        self.root.mkdir()
        seed_workspace(self.root)
        self.audit = self.root.parent / "audit.jsonl"

    def guard(self, runner):
        return Guard(self.root, runner, CommandPolicy(), self.audit, allowed_writes=("app.py",))

    def test_precheck_and_simulation_never_dispatch(self):
        runner = FakeRunner(lambda: self.fail("Runner must not be called"))
        guard = self.guard(runner)
        self.assertEqual(guard.run("rm -rf /").decision, "BLOCK")
        self.assertEqual(guard.run("aws ec2 run-instances --instance-type m5.large --count 3").decision, "BUDGET_BLOCK")
        self.assertEqual(guard.run("aws ec2 run-instances --instance-type m5.large --count 2").decision, "ALLOW")
        self.assertEqual(runner.calls, 0)

    def test_rollback_recovers_content_and_does_not_follow_symlink(self):
        protected = self.root / "wrangler.toml"
        outside = self.root.parent / "outside.txt"
        outside.write_text("external", encoding="utf-8")
        before = snapshot(self.root)

        def mutate():
            protected.unlink()
            protected.symlink_to(outside)
            (self.root / "notes.txt").unlink()
            (self.root / "extra").mkdir()
            (self.root / "extra" / "output.txt").touch()
            return Execution("changed", 0)

        result = self.guard(FakeRunner(mutate)).run("true")
        self.assertEqual(result.decision, "POST_VIOLATION")
        self.assertTrue(result.event["rollback_success"])
        self.assertEqual(snapshot(self.root), before)


        self.assertEqual(outside.read_text(), "external")
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([event["phase"] for event in audit], ["before_execution", "finished"])
        self.assertEqual(audit[-1]["run_id"], audit[0]["run_id"])

    def test_explicit_empty_declaration_and_interrupted_write(self):
        before = snapshot(self.root)

        def mutate():
            (self.root / "app.py").write_text("changed", encoding="utf-8")
            return Execution("", 0)

        result = self.guard(FakeRunner(mutate)).run("true", declared_writes=())
        self.assertEqual(result.decision, "POST_VIOLATION")
        self.assertEqual(snapshot(self.root), before)

        def interrupt():
            mutate()
            return Execution("timed out", 124, interrupted=True)

        result = self.guard(FakeRunner(interrupt)).run("true")
        self.assertEqual(result.decision, "ERROR")
        self.assertTrue(result.event["rollback_success"])
        self.assertEqual(snapshot(self.root), before)


class ObservedViolationTests(unittest.TestCase):
    def test_replay_starts_from_state_immediately_before_first_violation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / SOURCE_FILE).parent.mkdir(parents=True)
            (workspace / SOURCE_FILE).write_text("prior edit")
            (workspace / PROTECTED_FILE).write_text("original")
            command = "printf 'unexpected' >> setup.cfg"

            def execute(runner, actual_command, *, timeout=10):
                self.assertEqual(actual_command, command)
                self.assertEqual((runner.workspace / SOURCE_FILE).read_text(), "prior edit")
                config = runner.workspace / PROTECTED_FILE
                config.write_text(config.read_text() + "unexpected")
                return Execution("", 0)

            with patch.object(DockerRunner, "run", autospec=True, side_effect=execute):
                observed = observe_command(workspace, DockerRunner(workspace), command,
                                           root / "audit.jsonl", SOURCE_FILE, PROTECTED_FILE)
                replay = replay_observed_violation(root, root / "replay", "unused")
            self.assertEqual(observed["violations"], [PROTECTED_FILE])
            self.assertEqual((workspace / PROTECTED_FILE).read_text(), "originalunexpected")
            self.assertTrue(replay["without-dog"]["matches_observed_effect"])
            self.assertEqual(replay["with-dog"]["decision"], "POST_VIOLATION")
            self.assertTrue(replay["with-dog"]["workspace_restored"])
            self.assertTrue(replay["passed"])
