"""Machine-readable header: `---` fenced `key: value` lines; values starting with [ or { are one-line JSON (DESIGN §1.4)."""
import json


class HeaderError(ValueError):
    pass


def parse(text: str) -> tuple[dict, str]:
    lines = text.split("\n")  # not splitlines(): it also splits on   and friends
    if lines[0].rstrip("\r") != "---":
        return {}, text
    header = {}
    for i in range(1, len(lines)):
        n, line = i + 1, lines[i].rstrip("\r")
        if line == "---":
            return header, "\n".join(lines[i + 1:])
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise HeaderError(f"line {n}: expected 'key: value'")
        if key in header:
            raise HeaderError(f"line {n}: duplicate key {key!r}")
        if value.startswith(("[", "{")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as e:
                raise HeaderError(f"line {n}: bad JSON for {key!r}: {e.msg}") from None
        header[key] = value
    raise HeaderError("line 1: header opened with '---' but never closed")


def render(header: dict, body: str) -> str:
    out = ["---\n"]
    for k, v in header.items():
        if not isinstance(k, str) or not k or k != k.strip() or ":" in k or "\n" in k:
            raise HeaderError(f"bad header key {k!r}")
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        elif not isinstance(v, str) or v != v.strip() or "\n" in v or v.startswith(("[", "{")):
            # parse() would hand back something else; refuse instead of silently changing the value
            raise HeaderError(f"{k}: value {v!r} does not round-trip; use a plain string, list or dict")
        out.append(f"{k}: {v}\n")
    out.append("---\n")
    return "".join(out) + body
