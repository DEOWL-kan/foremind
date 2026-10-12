"""Codex CLI as the cross-vendor reviewer (m2f REQ-13): no seats. The flags, the strict schema and the usage fields are
as measured on codex-cli 0.161.0 before m2f.10 started; `codex exec --sandbox read-only` writes no file, while `-o`
(written by the CLI itself) still holds the last message."""
import json

# built-in tools this launch does not turn off (help lists no feature names for --disable); the user's MCP servers go
# with --ignore-user-config, apply_patch and exec_command are held by the read-only sandbox
UNRESTRICTED = ("web__run", "image_gen", "collaboration.spawn_agent")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
              "reasoning_output_tokens")


def strict_schema(schema) -> dict:
    """`--output-schema` takes strict mode only: every object gets additionalProperties false and all its properties
    required, those that were optional become anyOf [their definition, null]."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("properties", "items")}
    if "items" in schema:
        out["items"] = strict_schema(schema["items"])
    if "properties" in schema:
        req = set(schema.get("required", ()))
        out["properties"] = {k: strict_schema(v) if k in req else {"anyOf": [strict_schema(v), {"type": "null"}]}
                             for k, v in schema["properties"].items()}
        out["required"] = list(schema["properties"])
        out["additionalProperties"] = False
    return out


def argv(model, effort, mat, schema_path, out_path, prompt) -> list:
    """Read-only, in the materials directory, without the user's config (MCP servers) or rules; no --add-dir, no
    bypass, approval or profile flags."""
    return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--sandbox", "read-only",
            "--skip-git-repo-check", "-C", str(mat), "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "--output-schema", str(schema_path), "--json", "-o", str(out_path), prompt]


def usage(stdout_text) -> dict | None:
    """USAGE_KEYS of the last turn.completed in the `--json` event stream (a key not reported: None); None without
    one."""
    last = None
    for line in stdout_text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            last = e["usage"]
    return None if last is None else {k: last.get(k) for k in USAGE_KEYS}
