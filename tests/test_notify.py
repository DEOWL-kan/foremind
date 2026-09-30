import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from foremind import events, notify
from foremind.events import EventLog


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.got.append((self.path, body))
        self.send_response(self.server.code)
        self.end_headers()

    def log_message(self, *a):
        pass


def dead_url() -> str:
    """A local port nobody listens on: connections are refused at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


class NtfyTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.code, self.server.got = 200, []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        # never out through the developer's proxy (M-2): no proxy from the environment or the system, and macOS
        # system proxy exceptions must not decide which route a test takes
        self.enterContext(mock.patch.dict(os.environ))
        for k in [k for k in os.environ if k.lower().endswith("_proxy")]:
            del os.environ[k]
        self.enterContext(mock.patch("urllib.request.getproxies", return_value={}))
        self.enterContext(mock.patch("urllib.request.proxy_bypass", return_value=False))
        # S-5: a user config whose server nobody listens on, so a forgotten server never falls back to ntfy.sh
        self.user_config(f'[notify.ntfy]\nserver = "{dead_url()}"\n')
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def query(self, path):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))

    def test_success_is_an_http_2xx(self):
        self.assertTrue(notify.Ntfy(self.url, "t0pic").send("Q-3 待决", "选项 1/2", "P0"))
        path, body = self.server.got[0]
        self.assertTrue(path.startswith("/t0pic?"))
        self.assertEqual(self.query(path), {"title": "Q-3 待决", "priority": "5"})
        self.assertEqual(body, "选项 1/2")
        self.server.code = 500
        self.assertFalse(notify.Ntfy(self.url, "t0pic").send("x", "y"))
        self.assertFalse(notify.Ntfy(self.url, None).send("x", "y"), "no topic: nothing to send to")

    def test_proxy_failure_falls_back_to_direct(self):
        self.assertTrue(notify.Ntfy(self.url, "t0pic", proxy=dead_url()).send("x", "y"))
        self.assertTrue(self.server.got[0][0].startswith("/t0pic?"), "reached directly")

    def test_direct_failure_falls_back_to_the_proxy(self):
        target = dead_url()
        self.assertTrue(notify.Ntfy(target, "t0pic", proxy=self.url, route="direct").send("x", "y"))
        self.assertTrue(self.server.got[0][0].startswith(f"{target}/t0pic?"), "the fake proxy got the absolute URL")

    def user_config(self, text):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / "config.toml").write_text(text)
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(home)}))

    def test_unsent_once_per_key_and_the_topic_stays_out_of_events(self):
        root = self.root
        (root / ".foremind").mkdir()
        (root / ".foremind" / "ntfy_topic").write_text("s3cret-topic\n")
        self.user_config(f'[notify]\nproxy = "{dead_url()}"\n[notify.ntfy]\nserver = "{dead_url()}"\n')
        cfg = {"notify.channel": "ntfy"}
        ad = notify.get(root, cfg)
        self.assertEqual((ad.topic, ad.routes()[1]), ("s3cret-topic", {}))
        self.assertIs(notify.notify(root, cfg, "ev-1", "t", "b"), False)
        self.assertIsNone(notify.notify(root, cfg, "ev-1", "t", "b"), "same event id: never again")
        evs = list(EventLog(root / ".foremind" / "events.jsonl").iter())
        self.assertEqual([(e["type"], e["phase"]) for e in evs],
                         [("notify", "intent"), ("notify", "result"), ("notify_unsent", "result")])
        self.assertIs(evs[1]["sent"], False)
        self.assertNotIn("s3cret", json.dumps(evs))
        self.user_config(f'[notify.ntfy]\nserver = "{self.url}"\n')
        self.assertIs(notify.notify(root, cfg, "ev-2", "t", "b"), True)
        self.assertEqual(len(self.server.got), 1)

    def test_server_and_proxy_come_from_the_user_config_only(self):  # SF-10, §20 I52④
        self.user_config(f'[notify.ntfy]\nserver = "{self.url}"\n')
        cfg = {"notify.channel": "ntfy", "notify.ntfy": {"server": "http://attacker.invalid"},
               "notify.proxy": "http://attacker.invalid:1"}  # what a project layer could have merged in
        ad = notify.get(self.root, cfg)
        self.assertEqual(ad.url, self.url)
        self.assertNotIn("attacker", json.dumps(ad.routes()))

    def test_none_channel_and_unknown_channel(self):
        self.assertTrue(notify.get(self.root, {}).send("t", "b"))
        with self.assertRaises(ValueError):
            notify.get(self.root, {"notify.channel": "pager"})

    def test_no_proxy_from_the_environment_in_tests(self):  # M-2
        with mock.patch.dict(os.environ, {"https_proxy": "http://attacker.invalid:1"}):
            self.assertEqual(notify.get(self.root, {"notify.channel": "ntfy"}).routes(), [{}])


class Rec:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="P1"):
        self.sent.append((title, body, priority))
        return True


class PolicyTest(unittest.TestCase):  # m2b.6, §11.2
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".foremind").mkdir()
        self.rec = Rec()

    def events(self, type):
        return [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == type]

    def send(self, key, priority, cfg=None, at=None):
        at = at or time.time()
        with mock.patch.object(notify.time, "time", return_value=at), \
                mock.patch.object(events, "_now", lambda: datetime.fromtimestamp(at, timezone.utc).isoformat()):
            return notify.notify(self.root, cfg or {}, key, f"t-{key}", "b", priority, adapter=self.rec)

    def test_p0_two_per_twelve_hours_then_held_and_counted_in_the_next(self):
        self.assertEqual([self.send(k, "P0") for k in ("a", "b", "c", "d")], [True, True, False, False])
        self.assertIsNone(self.send("c", "P0"), "a held key counts as handled")
        self.assertEqual([(e["key"], e["title"]) for e in self.events("notify_held")], [("c", "t-c"), ("d", "t-d")])
        self.assertTrue(self.send("p1", "P1"), "P1 is not limited")
        later = time.time() + 12 * 3600 + 60
        self.assertTrue(self.send("e", "P0", at=later))
        self.assertEqual(self.rec.sent[-1], ("t-e", "b\n另有 2 条告警未单独推送，见运行报告", "P0"))
        self.assertTrue(self.send("f", "P0", at=later))
        self.assertEqual(self.rec.sent[-1][1], "b", "counted once")
        self.assertIs(self.send("g", "P0", at=later), False)

    def test_an_unsent_p0_does_not_count(self):
        self.rec.send = lambda *a: False
        self.assertEqual([self.send(k, "P0") for k in "abc"], [False] * 3)
        self.assertEqual(self.events("notify_held"), [])

    def test_p1_to_the_report_when_asked(self):
        cfg = {"notify.p1": "report"}
        self.assertIs(self.send("a", "P1", cfg), False)
        self.assertIsNone(self.send("a", "P1", cfg))
        self.assertEqual([(e["key"], e["title"]) for e in self.events("notify_deferred")], [("a", "t-a")])
        self.assertTrue(self.send("run_report:x", "P1", cfg), "the report's own summary goes out")
        self.assertTrue(self.send("b", "P0", cfg))
        self.assertEqual([t for t, _, _ in self.rec.sent], ["t-run_report:x", "t-b"])
        self.assertTrue(self.send("c", "P1"), "default: push")
        self.assertTrue(self.send("d", "P1", {"notify.p1": "push"}))

    def test_deferred_tells_a_held_or_deferred_key_from_a_failed_send(self):  # REQ-16
        self.send("a", "P1", {"notify.p1": "report"})
        self.send("b", "P0")
        self.send("c", "P0")
        self.send("d", "P0")  # the third P0 in 12 hours: held
        self.rec.send = lambda *a: False
        self.send("e", "P1")  # unsent
        self.assertEqual([notify.deferred(self.root, k) for k in "abcdef"], [True, False, False, True, False, False])


if __name__ == "__main__":
    unittest.main()
