"""Notifications, sending side (DESIGN §8.7, §10.1, §11.2): adapters with `send(title, body, priority) -> bool`.

`none` sends nothing (and says it succeeded); `ntfy` POSTs the body to `<server>/<topic>` with title and priority as
query parameters (UTF-8 safe, unlike headers). Success = an HTTP 2xx. The topic lives in `.foremind/ntfy_topic` and
is never printed or put in events. Routes: with a proxy known (`notify.proxy`, else the environment's) the first
route is `notify.route` (proxy | direct, default proxy) and the other one is tried when it fails.
notify() sends once per key (the id of the event it is about, §11.2) with an intent and a result event; a message
that went out on no route gets a `notify_unsent` event (for the morning report). Notifications carry ids, options and
a short line only: no paths, no code (ntfy.sh caches messages in clear).
Config (merged): notify.channel (none | ntfy, default none), notify.route. From the user config file only, like
statusline.command (§20 I52④: a project's config must not send the topic elsewhere): notify.ntfy = {server}
(default https://ntfy.sh), notify.proxy.
"""
import http.client
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from foremind.events import EventLog
from foremind.paths import state_dir, user_config_dir

PRIORITY = {"P0": 5, "P1": 4, "P2": 3, "P3": 2}
TIMEOUT_S = 10


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
    channel = cfg.get("notify.channel", "none")
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
    if any(e["dedupe_id"] == did for e in log.iter()):
        return None
    ad = adapter or get(root, cfg)
    log.append("notify", phase="intent", dedupe_id=did, key=key, channel=ad.name, priority=priority, title=title)
    ok = bool(ad.send(title, body, priority))
    log.append("notify", dedupe_id=did, key=key, channel=ad.name, sent=ok)
    if not ok:
        log.append("notify_unsent", key=key, channel=ad.name, priority=priority, title=title)
    return ok
