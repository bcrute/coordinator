"""Session-scoped process and agent activity contracts."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from coordinator.process_activity import (
    ProcessActivityObserver,
    ProcessRecord,
    build_process_activity,
)


def record(
    pid: int,
    ppid: int,
    name: str,
    *,
    state: str = "S",
    started: int | None = None,
    argv: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
    structured_model: str | None = None,
) -> ProcessRecord:
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        name=name,
        state=state,
        start_ticks=started if started is not None else pid,
        argv=argv,
        model_environment=environment or {},
        structured_model=structured_model,
    )


def activity(records: list[ProcessRecord], root_pid: int = 100) -> dict[str, object]:
    return build_process_activity(
        {value.pid: value for value in records},
        root_pid=root_pid,
        session_id="session-one",
        observed_at=1_000.0,
        uptime=500.0,
        clock_ticks=10,
    )


class ProcessClassificationTests(unittest.TestCase):
    def test_managed_tree_tracks_background_agent_models_and_excludes_other_instances(
        self,
    ) -> None:
        result = activity(
            [
                record(
                    100,
                    1,
                    "node",
                    argv=("/usr/bin/node", "/usr/bin/codex", "--model", "gpt-5.6-sol"),
                ),
                record(101, 100, "codex", argv=("/opt/codex",)),
                record(102, 101, "codex-code-mode-host"),
                record(103, 101, "python3.14"),
                record(
                    104,
                    103,
                    "claude",
                    argv=(
                        "/usr/bin/claude",
                        "--model",
                        "opus",
                        "--prompt",
                        "private task text",
                    ),
                    environment={"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
                ),
                record(
                    999,
                    1,
                    "claude",
                    argv=("/usr/bin/claude", "--model", "haiku"),
                ),
            ]
        )

        agents = result["agents"]
        self.assertEqual(
            [(value["label"], value["pid"]) for value in agents],
            [("Codex", 101), ("Claude", 104)],
        )
        self.assertEqual(agents[0]["role"], "lead")
        self.assertEqual(agents[0]["model"], "gpt-5.6-sol")
        self.assertEqual(agents[0]["model_source"], "argument")
        self.assertEqual(agents[1]["role"], "nested")
        self.assertEqual(agents[1]["model"], "opus")
        self.assertEqual(agents[1]["subagent_model"], "sonnet")

        terminals = result["background_terminals"]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["pid"], 103)
        self.assertEqual(terminals[0]["title"], "python3.14")
        self.assertEqual(terminals[0]["agent_count"], 1)
        self.assertEqual(terminals[0]["process_count"], 2)
        self.assertEqual(agents[1]["background_terminal_id"], terminals[0]["id"])

        serialized = json.dumps(result)
        self.assertNotIn("private task text", serialized)
        self.assertNotIn("haiku", serialized)
        self.assertNotIn("999", serialized)

    def test_direct_nested_agent_is_also_a_background_terminal(self) -> None:
        result = activity(
            [
                record(100, 1, "codex", argv=("codex", "--model", "gpt-5.6-sol")),
                record(101, 100, "codex", argv=("codex", "--model", "gpt-5.6-luna")),
            ]
        )

        self.assertEqual(len(result["agents"]), 2)
        self.assertEqual(result["agents"][1]["role"], "nested")
        self.assertEqual(result["agents"][1]["model"], "gpt-5.6-luna")
        self.assertEqual(len(result["background_terminals"]), 1)
        terminal = result["background_terminals"][0]
        self.assertEqual(terminal["kind"], "agent")
        self.assertEqual(terminal["title"], "Codex")
        self.assertEqual(terminal["agent_count"], 1)

    def test_internal_helper_is_hidden_but_its_background_job_is_visible(self) -> None:
        result = activity(
            [
                record(100, 1, "codex", argv=("codex",)),
                record(101, 100, "codex-code-mode-host"),
                record(102, 101, "bash", state="R"),
                record(103, 102, "make", state="R"),
            ]
        )

        self.assertEqual([value["pid"] for value in result["agents"]], [100])
        self.assertEqual(
            [value["pid"] for value in result["background_terminals"]], [102]
        )
        self.assertEqual(result["background_terminals"][0]["process_count"], 2)
        self.assertEqual(result["background_terminals"][0]["os_state"], "running")

    def test_invalid_model_metadata_is_not_reported(self) -> None:
        result = activity(
            [
                record(
                    100,
                    1,
                    "claude",
                    argv=("claude", "--model", "not a model", "--prompt", "secret"),
                    environment={
                        "ANTHROPIC_MODEL": "also not valid",
                        "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
                    },
                )
            ]
        )

        self.assertIsNone(result["agents"][0]["model"])
        self.assertEqual(result["agents"][0]["model_source"], "not_reported")
        self.assertEqual(result["agents"][0]["subagent_model"], "sonnet")
        self.assertNotIn("secret", json.dumps(result))

    def test_codex_structured_session_model_is_used_when_arguments_omit_it(
        self,
    ) -> None:
        result = activity(
            [
                record(
                    100,
                    1,
                    "codex",
                    argv=("codex",),
                    structured_model="gpt-5.6-sol",
                )
            ]
        )

        self.assertEqual(result["agents"][0]["model"], "gpt-5.6-sol")
        self.assertEqual(result["agents"][0]["model_source"], "session_index")


class ProcessObserverTests(unittest.TestCase):
    def test_non_procfs_platform_reports_unsupported_without_global_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = ProcessActivityObserver(proc_root=Path(temporary) / "missing")
            result = observer.snapshot(42, "session-two")

        self.assertFalse(result["supported"])
        self.assertEqual(result["state"], "unsupported")
        self.assertEqual(result["root_pid"], 42)
        self.assertEqual(result["agents"], [])
        self.assertEqual(result["background_terminals"], [])

    def test_session_index_returns_latest_model_for_only_open_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "rollout_path TEXT, model TEXT, updated_at_ms INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?)",
                    [
                        ("/sessions/open.jsonl", "gpt-5.5", 1),
                        ("/sessions/other.jsonl", "must-not-win", 3),
                        ("/sessions/open.jsonl", "gpt-5.6-sol", 2),
                    ],
                )
                connection.commit()

            model = ProcessActivityObserver._model_from_session_index(
                [str(database)], ["/sessions/open.jsonl"]
            )

        self.assertEqual(model, "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
