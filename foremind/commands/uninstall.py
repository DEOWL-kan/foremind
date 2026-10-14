"""`foremind uninstall` (DESIGN §13.4): remove what init added and its records still match (marked blocks, hook
entries and status line equal to the install manifest, the wrapped status line, the registry line). An entry changed
since, or one without a readable manifest, is left in place and listed; `.foremind/` data stays."""
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
    print("Removed the hooks and status line Foremind's install records still match, its config blocks and the "
          "project's registration; what was left in place is listed below")
    for n in notes:
        print(f"注意：{n}")
    return 0
