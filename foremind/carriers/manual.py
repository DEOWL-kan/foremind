"""Manual carrier (DESIGN §5): prints what the user should run or paste. It can confirm nothing, so close()
never returns evidence: breaking the lock needs the user's confirmation (ExitEvidence how="user_confirmed").
The program starts nothing itself, so a seat that never showed a heartbeat gives its claim back directly (seat.py)."""
import sys

from foremind.carriers import Carrier, SessionState


class ManualCarrier(Carrier):
    name = "manual"

    def __init__(self, root, cfg=None, out=None):
        super().__init__(root, cfg)
        self.out = out or sys.stdout

    def create(self, session, launch):
        print(f"[foremind] 请在新终端执行（会话 {session}）：\n  {launch.shell()}", file=self.out)

    def send(self, session, text):
        print(f"[foremind] 请把下面的消息粘贴给会话 {session}：\n{text}", file=self.out)
        return None

    def read_state(self, session):
        return SessionState(alive=None, idle=None)

    def wait_idle(self, session, timeout_s):
        return False

    def close(self, session):
        print(f"[foremind] 请关闭会话 {session}；确认它已退出后再回答待决。", file=self.out)
        return None

    def list_sessions(self):
        return None
