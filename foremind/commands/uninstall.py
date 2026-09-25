"""`foremind uninstall` (DESIGN §13.4): remove only what init added (marked blocks, our hook entries, the wrapped
status line, the registry line); files that existed before get their bytes back. `.foremind/` data stays."""
import sys

from foremind import install
from foremind.install import settings, tomlblock
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    sub.add_parser("uninstall", help="remove Foremind's hooks and config blocks from this project (data stays)"
                   ).set_defaults(func=_run)


def _run(args):
    try:
        notes = install.uninstall(find_project_root())
    except (ProjectNotFound, install.InstallError, settings.SettingsError, tomlblock.BlockError, OSError) as e:
        print(f"foremind uninstall: {e}", file=sys.stderr)
        return 1
    print("已移除钩子、状态栏串联、配置块与项目登记")
    for n in notes:
        print(f"注意：{n}")
    return 0
