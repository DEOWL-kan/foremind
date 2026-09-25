"""Agent vendors: each module builds the launch spec of its CLI for a seat. Only Claude Code so far; Codex is M2-7."""
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Launch:
    argv: list
    env: dict  # overlay on the carrier's environment
    cwd: Path

    def shell(self) -> str:
        """One shell line for carriers that take a command string (orca, manual)."""
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in self.env.items())
        return f"cd {shlex.quote(str(self.cwd))} && env {env} {shlex.join(self.argv)}"


def get(name):
    if name == "claude":
        from foremind.vendors import claude
        return claude
    raise NotImplementedError(f"vendor {name!r}: only claude seats for now (codex launch: M2-7)")
