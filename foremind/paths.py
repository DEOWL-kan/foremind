"""Project root and state/config locations (DESIGN §1.3)."""
import os
from pathlib import Path


class ProjectNotFound(Exception):
    pass


def find_project_root(start: Path | None = None) -> Path:
    env = os.environ.get("FOREMIND_PROJECT")
    if env:
        return Path(env).resolve()
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / ".foremind").is_dir() or (d / "foremind.toml").is_file():
            return d
    raise ProjectNotFound(f"no .foremind/ or foremind.toml at or above {here}")


def state_dir(root) -> Path:
    return Path(root) / ".foremind"


def user_config_dir() -> Path:
    env = os.environ.get("FOREMIND_CONFIG_HOME")
    return Path(env) if env else Path.home() / ".config" / "foremind"
