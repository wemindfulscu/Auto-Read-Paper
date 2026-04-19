"""Sanitize a CUSTOM_CONFIG payload before it lands at config/custom.yaml.

Reads YAML from stdin, enforces:
  - parseable via yaml.safe_load (rejects !!python/obj, custom tags, aliases)
  - no string value contains an OmegaConf interpolation (${...}), which
    would otherwise let a tampered repo variable exfiltrate env secrets
    like LLM_API_KEY / SENDER_PASSWORD through OmegaConf's resolver
  - no embedded CR/LF in string values (defensive)
  - size cap so a megabyte of payload can't wedge the parser

On success, writes the re-emitted YAML (via yaml.safe_dump) to the path
given as argv[1]. On any violation, prints an error and exits 1.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB is plenty for a config override
INTERP_RE = re.compile(r"\$\{[^}]*\}")


def _walk_strings(node, path="root"):
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise ValueError(f"Non-string key at {path}: {k!r}")
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sanitize_custom_config.py <output_path>", file=sys.stderr)
        return 2

    out_path = Path(sys.argv[1])
    payload = sys.stdin.read()

    if not payload.strip():
        print("sanitize: CUSTOM_CONFIG is empty, nothing to write", file=sys.stderr)
        return 0

    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        print(
            f"sanitize: CUSTOM_CONFIG exceeds {MAX_PAYLOAD_BYTES} bytes — refusing",
            file=sys.stderr,
        )
        return 1

    try:
        data = yaml.safe_load(payload)
    except yaml.YAMLError as exc:
        print(f"sanitize: CUSTOM_CONFIG is not valid YAML: {exc}", file=sys.stderr)
        return 1

    if data is None:
        print("sanitize: CUSTOM_CONFIG parsed to null", file=sys.stderr)
        return 0

    if not isinstance(data, (dict, list)):
        print(
            f"sanitize: top-level CUSTOM_CONFIG must be a mapping or list, got {type(data).__name__}",
            file=sys.stderr,
        )
        return 1

    for path, value in _walk_strings(data):
        if INTERP_RE.search(value):
            print(
                f"sanitize: refusing ${{...}} interpolation in CUSTOM_CONFIG at {path!r} "
                f"(would allow env-secret exfiltration via OmegaConf)",
                file=sys.stderr,
            )
            return 1
        if "\r" in value or "\n" in value and path.endswith(("sender", "receiver", "smtp_server")):
            print(f"sanitize: refusing CR/LF in {path!r}", file=sys.stderr)
            return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    print(f"sanitize: wrote sanitized YAML to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
