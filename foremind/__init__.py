__version__ = "0.0.1"  # the only version text: pyproject.toml reads it (REQ-1)

# the packaged templates (REQ-1), relative to foremind/templates/
TEMPLATES = ("PROTOCOL.md", "handoff.md", "controller-handoff.md",
             *(f"roles/{r}.md" for r in ("planner", "seat", "controller", "decider", "cataloger", "auditor", "reviewer")))


def template_path(name):
    """foremind/templates/<name> where the package is installed: a lasting path for a package on disk, never a
    temporary copy."""
    from importlib.resources import files
    return files(__name__) / "templates" / name


def template(name) -> str:
    """The text of foremind/templates/<name>. ValueError when it is missing, blank or unreadable, so no role starts
    without its card or falls back to a shorter prompt."""
    p = template_path(name)
    fix = "reinstall Foremind from a complete wheel (pip install --force-reinstall <wheel>)"
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"packaged template {p} cannot be read ({e.__class__.__name__}: {e}); {fix}") from None
    if not text.strip():
        raise ValueError(f"packaged template {p} is blank; {fix}")
    return text
