"""Carrier adapters (DESIGN §5): create, send, read_state, wait_idle, close, plus list_sessions.

Sessions are addressed by their Foremind name (`fm-...`, only [A-Za-z0-9_-], so every carrier can use it as is).
create() never reuses a session: a name the carrier already has raises SessionExists (M1-4 r1 MF-4).
Programs call deliver(), not send(): it records a `program_delivery` intent (time + sha256 of the normalized text,
§10.8, §20 I42) before sending, so UserPromptSubmit can tell program input from the user's, and a result after it.
close() returns lock.ExitEvidence only when the carrier has confirmed the session is gone and so has its process
(m2d.1, REQ-1): _settled() hands the carrier's evidence to sessions.settle, which sends the session's claude SIGTERM if
it is still there (an Orca tab closed while its pty lives on, a tmux pane's child left behind) and waits; else None.
A process whose identity cannot be checked (ps fails, …) gets nothing, the evidence stands (session_close_unverified).
"""
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from foremind import sessions
from foremind.events import EventLog
from foremind.fsutil import sha256_bytes
from foremind.paths import state_dir


class CarrierError(Exception):
    pass


class SessionExists(CarrierError):
    pass


@dataclass
class SessionState:
    alive: bool | None  # None: the carrier cannot tell (manual, or a session it has no record of)
    idle: bool | None
    tail: list = field(default_factory=list)  # last non-empty screen lines, for a rough read


def normalize(text) -> str:
    """Text as delivered and hashed (§20 I42): CRLF and CR -> LF, surrounding whitespace stripped. UserPromptSubmit
    normalizes the prompt the same way before comparing hashes."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def run(argv, input=None, timeout=60):
    try:
        return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CarrierError(f"{' '.join(argv[:3])}: no answer within {timeout} s") from None
    except OSError as e:
        raise CarrierError(f"{argv[0]}: {e}") from None


class Carrier:
    name = ""
    exit_timeout_s = 15
    ps = None  # sessions.snapshot's ps: a callable returning `ps -Ao` text (tests); None is /bin/ps

    def __init__(self, root, cfg=None):
        self.root = Path(root)
        self.cfg = cfg or {}

    def create(self, session, launch) -> None:
        """Start `launch` (vendors.Launch) as session `session`; SessionExists if the carrier already has that name."""
        raise NotImplementedError

    def send(self, session, text) -> bool | None:
        """True = the carrier saw the input submitted; None = sent, not confirmed (§2.5)."""
        raise NotImplementedError

    def read_state(self, session) -> SessionState:
        raise NotImplementedError

    def wait_idle(self, session, timeout_s) -> bool:
        raise NotImplementedError

    def close(self, session):
        raise NotImplementedError

    def _settled(self, session, evidence):
        """The carrier's evidence unless the session's process runs on after SIGTERM (sessions.settle), else None."""
        if evidence is None or sessions.settle(self.root, session, ps=self.ps, timeout_s=self.exit_timeout_s):
            return evidence
        return None

    def list_sessions(self) -> list | None:
        """Session names the carrier knows; None when it cannot list (manual)."""
        raise NotImplementedError

    def deliver(self, session, text) -> bool | None:
        text = normalize(text)
        log = EventLog(state_dir(self.root) / "events.jsonl")
        did = uuid.uuid4().hex
        log.append("program_delivery", phase="intent", dedupe_id=did, session=session, carrier=self.name,
                   text_sha256=sha256_bytes(text.encode("utf-8")))
        try:
            confirmed = self.send(session, text)
        except Exception as e:
            log.append("program_delivery", dedupe_id=did, session=session, ok=False, error=str(e))
            raise
        log.append("program_delivery", dedupe_id=did, session=session, ok=True, confirmed=confirmed)
        return confirmed


def get(kind, root, cfg=None) -> Carrier:
    """`cfg`: the merged config from config.load (`[carrier.tmux] socket` and the like)."""
    from foremind.carriers import manual, orca, tmux
    kinds = {"tmux": tmux.TmuxCarrier, "orca": orca.OrcaCarrier, "manual": manual.ManualCarrier}
    if kind not in kinds:
        raise CarrierError(f"unknown carrier {kind!r} (known: {sorted(kinds)})")
    return kinds[kind](root, cfg)
