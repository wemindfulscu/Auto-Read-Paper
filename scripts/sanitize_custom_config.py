"""Sanitize a CUSTOM_CONFIG payload before it lands at config/custom.yaml.

Reads YAML from stdin, enforces:
  - parseable via yaml.safe_load (rejects !!python/obj, custom tags, aliases)
  - the only OmegaConf interpolation allowed in string values is the
    documented ${oc.env:VAR} / ${oc.env:VAR,default} form. Anything else
    (${oc.decode:...}, ${env:...}, nested ${${...}}, custom resolvers)
    is refused — those are the shapes a tampered repo variable could
    use to exfiltrate env secrets like LLM_API_KEY / SENDER_PASSWORD.
  - no embedded CR/LF in string values (defensive against header injection)
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

# Any ${...} occurrence — used for the deny pass.
INTERP_RE = re.compile(r"\$\{[^}]*\}")

# The single allowed interpolation shape: ${oc.env:VAR} or
# ${oc.env:VAR,default}. VAR must be a normal env-var identifier and the
# default (if present) must not itself contain another ${...}.
SAFE_OC_ENV_RE = re.compile(
    r"^\$\{oc\.env:[A-Za-z_][A-Za-z0-9_]*(?:,[^${}]*)?\}$"
)


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


def _interpolations_are_safe(value: str) -> tuple[bool, str | None]:
    """True iff every ${...} in value is a plain ${oc.env:VAR[,default]}."""
    for match in INTERP_RE.finditer(value):
        if not SAFE_OC_ENV_RE.match(match.group(0)):
            return False, match.group(0)
    return True, None


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
        ok, bad = _interpolations_are_safe(value)
        if not ok:
            print(
                f"sanitize: refusing unsafe interpolation {bad!r} at {path!r} "
                f"(only ${{oc.env:VAR}} / ${{oc.env:VAR,default}} is allowed)",
                file=sys.stderr,
            )
            return 1
        if "\r" in value or "\n" in value:
            print(f"sanitize: refusing CR/LF in {path!r}", file=sys.stderr)
            return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    print(f"sanitize: wrote sanitized YAML to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

