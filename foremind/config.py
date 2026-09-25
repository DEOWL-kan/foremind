"""Layered config with four key classes (DESIGN §1.5).

merge()/load() return a flat dict keyed by dotted names (`delivery.repo.backend.merge_method`);
AUTHZ keys map to their effective value. Unregistered keys are AUTHZ.
Registered patterns must not be prefixes of each other: a dict at a registered key is one value.
Per-repo delivery keys live under `delivery.repo.<repo-id>.` so a repo id can never collide with `delivery.level` & co.

AUTHZ rules: with an order (a list, strictest first, or a numeric direction) a ceiling can only tighten and a value
must stay within it. Without an order only the user layer may set a key; other layers must repeat the same value
(an approved task may change the value, never the ceiling).
"""
import copy
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from foremind.paths import state_dir, user_config_dir

PLAIN, UNION, REPO_CONVENTION, AUTHZ = "plain", "union", "repo_convention", "authz"
LAYERS = ("user", "project", "delivery", "task")

# `*` matches exactly one dotted segment (a repo id, a tier, ...).
KEY_CLASSES: dict[str, str] = {
    "carrier.*": PLAIN,
    "notify.*": PLAIN,
    "runtime.*": PLAIN,
    "context.*": PLAIN,  # §6.3 effective budget: window_tokens, soft_pct, hard_pct, abs_cap_tokens (§20 I15)
    "plan.coupling.*": PLAIN,  # §4.2 5d weights and thresholds
    "project.name": PLAIN,  # §20 I39 slug source; keep it stable once batches are running
    "seat.*": PLAIN,  # sessionstart/verify/manual timeouts, continue_enabled (§20 I44)
    "hard_block.patterns": UNION,
    "supervisor.*": PLAIN,  # tick_s, max_seats, max_oneshot, seat_retries, gate_retry_min, merged_check_min
    "quota.stale_min": PLAIN,
    "quota.backoff_min": PLAIN,
    "review.max_failures": PLAIN,  # consecutive reviewer failures on the same heads before `failed`
    "routes.*.*": PLAIN,  # e.g. routes.reviewer.model / effort (§3.2)
    "oneshot.timeout_min": PLAIN,
    "acceptance.timeout_min": PLAIN,
    "flow.*.*": PLAIN,
    "review.max_rounds": PLAIN,
    "stuck.*": PLAIN,
    "delivery.depends_on": PLAIN,
    "exclude.models": UNION,
    "exclude.providers": UNION,
    "hard_block.categories": UNION,
    "forbidden.actions": UNION,
    "gate.checks": UNION,
    "repos": REPO_CONVENTION,
    "gate.ci": REPO_CONVENTION,
    "delivery.repo.*.target_branch": REPO_CONVENTION,
    "delivery.repo.*.merge_method": REPO_CONVENTION,
    "delivery.repo.*.update_method": REPO_CONVENTION,
    "delivery.repo.*.keep_updated": REPO_CONVENTION,
    "delivery.repo.*.ci": REPO_CONVENTION,
    "delivery.repo.*.merge_command": REPO_CONVENTION,
    "delivery.level": AUTHZ,
    "delivery.repo.*.level": AUTHZ,
    "gate.rereview_after_update": AUTHZ,
    "quota.reserve_pct": AUTHZ,
    "seat.permission_mode": AUTHZ,
    "delivery.repo.*.push_pr": AUTHZ,
    "quota.low_pct": AUTHZ,
    "quota.recover_pct": AUTHZ,
    "quota.oneshot_pause_pct": AUTHZ,
    "quota.probe_command": AUTHZ,  # no order: user layer only, a project must not inject a command
    "notify.ntfy": AUTHZ,  # no order: user layer only (§20 I52④), a project must not redirect the topic
    "notify.proxy": AUTHZ,  # §8.1 #23: who pushes and opens PRs
    "hard_block.allow_tools": AUTHZ,  # no order: user layer only (§20 I35)
    "statusline.command": AUTHZ,  # no order: user layer only, a project must not inject a command
}

HIGHER_STRICTER, LOWER_STRICTER = "higher_stricter", "lower_stricter"  # numeric directions

# A list is strictest first; HIGHER_STRICTER / LOWER_STRICTER order numbers. AUTHZ keys without an order: see module doc.
AUTHZ_ORDERS: dict[str, list | str] = {
    "delivery.level": ["done", "merge_dev"],
    "delivery.repo.*.level": ["done", "merge_dev"],
    "gate.rereview_after_update": ["full", "delta"],
    "quota.reserve_pct": HIGHER_STRICTER,  # a bigger reserve is stricter
    "delivery.repo.*.push_pr": ["user", "system"],
    "quota.low_pct": LOWER_STRICTER,  # slow down earlier
    "quota.recover_pct": LOWER_STRICTER,  # recover later
    "quota.oneshot_pause_pct": LOWER_STRICTER,  # the user keeping #23 is stricter
    "seat.permission_mode": ["plan", "dontAsk", "manual", "acceptEdits", "auto", "bypassPermissions"],
}


class ConfigError(Exception):
    pass


@dataclass
class Layer:
    name: str
    data: dict
    user_approved: bool = False


def _lookup(table, key, default=None):
    if key in table:
        return table[key]
    parts = key.split(".")
    for pattern, v in table.items():
        p = pattern.split(".")
        if len(p) == len(parts) and all(a in ("*", b) for a, b in zip(p, parts)):
            return v
    return default


def key_class(key: str) -> str:
    return _lookup(KEY_CLASSES, key, AUTHZ)


def _is_authz_spec(v):
    return isinstance(v, dict) and bool(v) and v.keys() <= {"ceiling", "value"}


def _is_prefix(key):
    """True if some registered pattern continues below `key`, so `key` is a table, not a value."""
    parts = key.split(".")
    return any(len(p) > len(parts) and all(a in ("*", b) for a, b in zip(p, parts))
               for p in (pat.split(".") for pat in KEY_CLASSES))


def _flatten(data, prefix=""):
    out = {}
    for k, v in data.items():
        key = prefix + k
        # a registered key keeps its dict whole; {value, ceiling}-only tables are AUTHZ notation only where
        # the key is AUTHZ (or unregistered) and not the parent of registered keys (`[stuck] value = 5` is stuck.value)
        if isinstance(v, dict) and _lookup(KEY_CLASSES, key) is None and (not _is_authz_spec(v) or _is_prefix(key)):
            flat = _flatten(v, key + ".")
        else:
            flat = {key: v}
        for fk, fv in flat.items():
            if fk in out:  # dotted and nested spellings of one key
                raise ConfigError(f"config key {fk!r} is set twice (dotted and nested spelling)")
            out[fk] = fv
    return out


def _rank(order, x):
    """Larger = wider."""
    return order.index(x) if isinstance(order, list) else -x if order == HIGHER_STRICTER else x


def _merge_authz(state, key, spec, layer):
    where = f"{key} (layer {layer.name})"
    order = _lookup(AUTHZ_ORDERS, key)
    ceiling, value = (spec.get("ceiling"), spec.get("value")) if _is_authz_spec(spec) else (None, spec)
    c, v = state
    if order is None:
        for what, new, old in (("ceiling", ceiling, c), ("value", value, v if v is not None else c)):
            if new is None or new == old or layer.name == "user":
                continue
            if what == "value" and layer.name == "task" and layer.user_approved:
                continue
            raise ConfigError(f"{where}: {what} {new!r} differs from {old!r} and {key} has no order, "
                              "so only the user layer may set it")
        c, v = c if ceiling is None else ceiling, v if value is None else value
        if c is not None and v is not None and v != c:
            raise ConfigError(f"{where}: value {v!r} is wider than ceiling {c!r}")
        state[:] = [c, v]
        return v if v is not None else c

    for x in (ceiling, value):
        if x is None:
            continue
        if isinstance(order, list):
            if x not in order:
                raise ConfigError(f"{where}: {x!r} is not one of {order}")
        elif isinstance(x, bool) or not isinstance(x, (int, float)) or isinstance(x, float) and not math.isfinite(x):
            raise ConfigError(f"{where}: {x!r} is not a finite number")

    def wider(a, b):
        return _rank(order, a) > _rank(order, b)

    def default(c):  # unset value = strictest option (DESIGN §7.5); a numeric order has none, use the ceiling
        return order[0] if isinstance(order, list) else c

    if ceiling is not None:
        if c is not None and wider(ceiling, c):
            raise ConfigError(f"{where}: ceiling {ceiling!r} is wider than the upper layers' {c!r}")
        c = ceiling
    if value is not None:
        prev = v if v is not None else default(c)
        if layer.name == "task" and not layer.user_approved and (prev is None or wider(value, prev)):
            raise ConfigError(f"{where}: task widens {prev!r} -> {value!r} without user approval")
        v = value
    if v is not None and c is not None and wider(v, c):
        raise ConfigError(f"{where}: value {v!r} is wider than ceiling {c!r}")
    state[:] = [c, v]
    return v if v is not None else default(c)


def task_layer(header: dict) -> dict:
    """Task-layer data from a batch header: its `config` table, flattened, plus `hard_block` unioned into hard_block.categories."""
    cfg = header.get("config", {})
    hard = header.get("hard_block", [])
    if not isinstance(cfg, dict) or not isinstance(hard, list):
        raise ConfigError("task header: `config` must be a table and `hard_block` a list")
    out = _flatten(copy.deepcopy(cfg))
    if hard:
        cats = out.get("hard_block.categories", [])
        if "hard_block" in out or not isinstance(cats, list):
            raise ConfigError("task header: config.hard_block.categories must be a list")
        out["hard_block.categories"] = [*cats, *(x for x in hard if x not in cats)]
    return out


def merge(layers: list[Layer]) -> dict:
    for layer in layers:
        if layer.name not in LAYERS:
            raise ConfigError(f"unknown layer {layer.name!r}")
    out, authz = {}, {}
    for layer in sorted(layers, key=lambda x: LAYERS.index(x.name)):
        for key, v in _flatten(layer.data).items():
            cls = key_class(key)
            if cls == PLAIN:
                out[key] = v
            elif cls == UNION:
                if not isinstance(v, list):
                    raise ConfigError(f"{key} (layer {layer.name}): expected a list")
                cur = out.setdefault(key, [])
                for x in v:
                    if x not in cur:
                        cur.append(x)
            elif cls == REPO_CONVENTION:
                if layer.name == "task":
                    raise ConfigError(f"{key} (layer task): repo conventions cannot be set by a task")
                out[key] = v
            else:
                out[key] = _merge_authz(authz.setdefault(key, [None, None]), key, v, layer)
    return out


def load(root: Path, task: dict | None = None, *, task_user_approved=False) -> dict:
    """`task` is the task layer's data, normally `task_layer(batch_header)`."""
    root = Path(root)
    files = [
        ("user", user_config_dir() / "config.toml"),
        ("project", root / "foremind.toml"),
        ("project", state_dir(root) / "config.toml"),
        ("delivery", state_dir(root) / "delivery.toml"),
    ]
    layers = []
    for name, path in files:
        try:
            with open(path, "rb") as f:
                layers.append(Layer(name, tomllib.load(f)))
        except FileNotFoundError:
            pass
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ConfigError(f"{path} (layer {name}): {e}") from None
    if task:
        layers.append(Layer("task", task, task_user_approved))
    return merge(layers)
