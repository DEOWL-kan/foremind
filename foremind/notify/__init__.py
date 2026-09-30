"""Notifications, sending side (DESIGN §8.7, §10.1, §11.2): adapters with `send(title, body, priority) -> bool`.

`none` sends nothing (and says it succeeded); `ntfy` POSTs the body to `<server>/<topic>` with title and priority as
query parameters (UTF-8 safe, unlike headers). Success = an HTTP 2xx. The topic lives in `.foremind/ntfy_topic` and
is never printed or put in events. Routes: with a proxy known (`notify.proxy`, else the environment's) the first
route is `notify.route` (proxy | direct, default proxy) and the other one is tried when it fails.
notify() sends once per key (the id of the event it is about, §11.2) with an intent and a result event; a message
that went out on no route gets a `notify_unsent` event (for the run report). Notifications carry ids, options and
a short line only: no paths, no code (ntfy.sh caches messages in clear).
Policy (§11.2, m2b.6), both recorded instead of sent and returned as False, the key counting as handled: a P0 while
P0_MAX went out in the last P0_WINDOW_S -> `notify_held{key, title}` (the next P0 sent says how many were held; the
run report lists them; under a pause, supervisor/phases/report.paused); a P1 with notify.p1 = "report" -> `notify_deferred{key, title}` for the next run report, whose
own summary (key REPORT_KEY…) is sent anyway. deferred(root, key) tells the two apart from a failed send.
Config (merged): notify.channel (none | ntfy, default none), notify.route, notify.p1 (push | report, default push;
anything but "report" pushes). From the user config file only, like
statusline.command (§20 I52④: a project's config must not send the topic elsewhere): notify.ntfy = {server}
(default https://ntfy.sh), notify.proxy.
"""
import http.client
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from foremind.defaults import TABLE
from foremind.events import EventLog
from foremind.paths import state_dir, user_config_dir

PRIORITY = {"P0": 5, "P1": 4, "P2": 3, "P3": 2}
TIMEOUT_S = 10
P0_MAX, P0_WINDOW_S = 2, 12 * 3600  # §11.2: at most 2 P0 pushes per 12 hours
REPORT_KEY = "run_report:"  # the run report's own summary: never deferred to a run report


class NoneNotifier:
    name = "none"

    def send(self, title, body, priority="P1") -> bool:
        return True


class Ntfy:
    name = "ntfy"

    def __init__(self, server, topic, proxy=None, route=None):
        self.url, self.topic, self.proxy = server.rstrip("/"), topic, proxy
        self.proxy_first = route != "direct"

    def routes(self) -> list[dict]:
        if not self.proxy:
            return [{}]
        via = {"http": self.proxy, "https": self.proxy}
        return [via, {}] if self.proxy_first else [{}, via]

    def send(self, title, body, priority="P1") -> bool:
        if not self.topic:
            return False
        query = urllib.parse.urlencode({"title": title, "priority": PRIORITY.get(priority, 3)})
        url = f"{self.url}/{urllib.parse.quote(self.topic, safe='')}?{query}"
        for proxies in self.routes():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
            req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
            try:
                with opener.open(req, timeout=TIMEOUT_S) as r:
                    if 200 <= r.status < 300:
                        return True
            except urllib.error.HTTPError as e:  # answered, not 2xx: the next route
                e.close()
            except (OSError, ValueError, http.client.HTTPException):  # refused, timed out, bad URL
                pass
        return False


def topic(root) -> str | None:
    try:
        return (state_dir(root) / "ntfy_topic").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def user_notify() -> dict:
    """`[notify]` of the user config file; {} when missing or unreadable."""
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            v = tomllib.load(f).get("notify")
    except (OSError, ValueError):  # ValueError: TOMLDecodeError, UnicodeDecodeError
        return {}
    return v if isinstance(v, dict) else {}


def get(root, cfg):
    channel = cfg.get("notify.channel", TABLE["notify.channel"])
    if channel == "none":
        return NoneNotifier()
    if channel == "ntfy":
        env, user = urllib.request.getproxies(), user_notify()
        server = (user.get("ntfy") if isinstance(user.get("ntfy"), dict) else {}).get("server")
        proxy = user.get("proxy") if isinstance(user.get("proxy"), str) else None
        return Ntfy(server if isinstance(server, str) and server else "https://ntfy.sh", topic(root),
                    proxy or env.get("https") or env.get("http"), cfg.get("notify.route"))
    raise ValueError(f"unknown notify.channel {channel!r} (known: none, ntfy)")


def notify(root, cfg, key, title, body, priority="P1", *, adapter=None) -> bool | None:
    """Send once per `key`; None when the key was handled before (sent, unsent, or cut short by a crash)."""
    log, did = EventLog(state_dir(root) / "events.jsonl"), f"notify:{key}"
    evs = list(log.iter())
    if any(e["dedupe_id"] == did for e in evs):
        return None
    if priority == "P1" and cfg.get("notify.p1", TABLE["notify.p1"]) == "report" and not key.startswith(REPORT_KEY):
        log.append("notify_deferred", dedupe_id=did, key=key, title=title)
        return False
    if priority == "P0":
        sent, held = _p0(evs, time.time())
        if sent >= P0_MAX:
            log.append("notify_held", dedupe_id=did, key=key, title=title)
            return False
        if held:
            body += f"\n另有 {len(held)} 条告警未单独推送，见运行报告"
    ad = adapter or get(root, cfg)
    log.append("notify", phase="intent", dedupe_id=did, key=key, channel=ad.name, priority=priority, title=title)
    ok = bool(ad.send(title, body, priority))
    log.append("notify", dedupe_id=did, key=key, channel=ad.name, sent=ok)
    if not ok:
        log.append("notify_unsent", key=key, channel=ad.name, priority=priority, title=title)
    return ok


def deferred(root, key) -> bool:
    """Whether notify() held or deferred `key` (its False meant "recorded for the next push or run report", not
    "failed"; m2b.6 r3). A key is handled once, so its one notify_deferred / notify_held event is the latest."""
    return any(e["type"] in ("notify_deferred", "notify_held") and e.get("key") == key
               for e in EventLog(state_dir(root) / "events.jsonl").iter())


def held(evs) -> list:
    """The notify_held events since the last P0 sent: not carried out by a P0 yet."""
    return _p0(evs, 0)[1]


def _p0(evs, now) -> tuple[int, list]:
    """(P0 sent within the window, notify_held events since the last P0 sent)."""
    p0 = {e["dedupe_id"] for e in evs if e["type"] == "notify" and e["phase"] == "intent" and e.get("priority") == "P0"}
    sent, held = 0, []
    for e in evs:
        if e["type"] == "notify" and e["phase"] == "result" and e.get("sent") and e["dedupe_id"] in p0:
            held = []
            try:
                sent += now - datetime.fromisoformat(e["ts"]).timestamp() < P0_WINDOW_S
            except (TypeError, ValueError):
                sent += 1  # no readable time: counted, so a bad record never lets more through
        elif e["type"] == "notify_held":
            held.append(e)
    return sent, held
