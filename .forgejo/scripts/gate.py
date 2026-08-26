#!/usr/bin/env python3
"""The registry's tiered approval gate (runs BASE-REF via pull_request_target).

Request tier (self-merge): diff == exactly apps/<app>.yaml, author owns <app>
per owners.yaml, head validates against the platform schema, and every changed
field is request-tier within the platform catalog's bounds.
Operator tier (everything else): requires an APPROVED review, pinned to the PR
HEAD sha, from a user in owners.yaml `operators:`.

PR head content is only ever fetched as DATA (raw at the head sha) — never
checked out, never executed. Trusted inputs (owners.yaml, this script) come
from the BASE checkout; schema + catalog come from the platform repo @main.
"""
import json
import os
import re
import sys
import subprocess
import urllib.request

API = os.environ["API"]
REPO = os.environ["REPO"]
PR = os.environ["PR"]
AUTHOR = os.environ["AUTHOR"]
HEAD_SHA = os.environ["HEAD_SHA"]
TOKEN = os.environ["TOKEN"]

def die(msg: str) -> None:
    print(f"::error::gate: {msg}")
    sys.exit(1)

def api(path: str, raw: bool = False):
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"token {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if raw else json.loads(data)

def yaml_load(text_or_path: str, is_path: bool = True):
    """Parse YAML via the pinned yq binary (the runner image ships no pip, so
    no pyyaml): yq -o=json converts, stdlib json parses."""
    if is_path:
        out = subprocess.run(["yq", "-o=json", ".", text_or_path], capture_output=True, text=True)
    else:
        out = subprocess.run(["yq", "-o=json", "."], input=text_or_path, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"yq parse failed: {out.stderr.strip()}")
    return json.loads(out.stdout)

def validate_subset(spec, schema, path="$"):
    """Minimal JSON-Schema interpreter covering EXACTLY the keywords
    schema/appspec.json uses (type, required, properties,
    additionalProperties, enum, pattern, items, const). Unknown keywords are
    IGNORED-BY-NAME but asserted below so schema evolution past the subset
    fails loudly instead of silently not validating."""
    SUPPORTED = {"$schema", "$id", "title", "description", "type", "required",
                 "properties", "additionalProperties", "enum", "pattern",
                 "items", "const", "minimum", "maximum", "exclusiveMinimum",
                 "maxLength", "minLength", "_comment"}
    unknown = set(schema) - SUPPORTED
    if unknown:
        die(f"schema uses keywords outside the gate's validator subset: {sorted(unknown)} — extend gate.py")
    t = schema.get("type")
    if t:
        ok = {"object": dict, "array": list, "string": str, "boolean": bool}.get(t)
        if ok and not isinstance(spec, ok):
            return f"{path}: expected {t}"
        if t == "number" and not isinstance(spec, (int, float)):
            return f"{path}: expected number"
        if t == "integer" and not isinstance(spec, int):
            return f"{path}: expected integer"
    if "const" in schema and spec != schema["const"]:
        return f"{path}: must be {schema['const']!r}"
    if "enum" in schema and spec not in schema["enum"]:
        return f"{path}: {spec!r} not in {schema['enum']}"
    if "pattern" in schema and isinstance(spec, str) and not re.search(schema["pattern"], spec):
        return f"{path}: {spec!r} fails pattern {schema['pattern']}"
    if "minimum" in schema and isinstance(spec, (int, float)) and spec < schema["minimum"]:
        return f"{path}: {spec} < minimum {schema['minimum']}"
    if "maximum" in schema and isinstance(spec, (int, float)) and spec > schema["maximum"]:
        return f"{path}: {spec} > maximum {schema['maximum']}"
    if "exclusiveMinimum" in schema and isinstance(spec, (int, float)) and spec <= schema["exclusiveMinimum"]:
        return f"{path}: {spec} <= exclusiveMinimum {schema['exclusiveMinimum']}"
    if "maxLength" in schema and isinstance(spec, str) and len(spec) > schema["maxLength"]:
        return f"{path}: length {len(spec)} > maxLength {schema['maxLength']}"
    if "minLength" in schema and isinstance(spec, str) and len(spec) < schema["minLength"]:
        return f"{path}: length {len(spec)} < minLength {schema['minLength']}"
    if isinstance(spec, dict):
        for req in schema.get("required", []):
            if req not in spec:
                return f"{path}: missing required '{req}'"
        props = schema.get("properties", {})
        for k, v in spec.items():
            if k in props:
                err = validate_subset(v, props[k], f"{path}.{k}")
                if err:
                    return err
            elif schema.get("additionalProperties") is False:
                return f"{path}: unknown property '{k}'"
    if isinstance(spec, list) and "items" in schema:
        for i, item in enumerate(spec):
            err = validate_subset(item, schema["items"], f"{path}[{i}]")
            if err:
                return err
    return None

def fetch(url: str):
    req = urllib.request.Request(url, headers={"Authorization": f"token {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def operator_approval(operators: list[str]) -> bool:
    """APPROVED review from an operator, pinned to the current head sha.
    A review on an older sha does not count — the author could have pushed
    after the approval (this Forgejo does not reliably dismiss stale reviews)."""
    reviews = api(f"/repos/{REPO}/pulls/{PR}/reviews")
    for rv in reviews:
        if (
            rv.get("state") == "APPROVED"
            and rv.get("user", {}).get("login") in operators
            and rv.get("commit_id") == HEAD_SHA
            and not rv.get("dismissed", False)
            and not rv.get("stale", False)
        ):
            print(f"::notice::gate: operator approval by {rv['user']['login']} at {HEAD_SHA[:10]}")
            return True
    return False

def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = d
    return out

def main() -> None:
    owners_doc = yaml_load("owners.yaml")
    operators = owners_doc.get("operators") or []
    app_owners = owners_doc.get("apps") or {}

    files = api(f"/repos/{REPO}/pulls/{PR}/files?limit=100")
    paths = [(f.get("filename"), f.get("status", "modified")) for f in files]

    def need_operator(reason: str) -> None:
        print(f"::notice::gate: OPERATOR TIER — {reason}")
        if operator_approval(operators):
            print("gate: pass (operator-approved)")
            sys.exit(0)
        die(
            f"operator approval required ({reason}). An operator "
            f"({', '.join(operators)}) must submit an APPROVED review on the "
            f"current head, then re-run this gate (UI re-run or empty commit)."
        )

    if len(paths) != 1:
        need_operator(f"diff touches {len(paths)} files")
    path, status = paths[0]
    m = re.fullmatch(r"apps/([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.yaml", path or "")
    if not m:
        need_operator(f"'{path}' is not a single apps/<app>.yaml")
    # Forgejo reports an edited file as "changed" (GitHub says "modified").
    if status not in ("modified", "changed"):
        need_operator(f"apps file {status} (new/deleted specs are operator acts)")
    app = m.group(1)
    if AUTHOR not in (app_owners.get(app) or []):
        need_operator(f"author '{AUTHOR}' does not own app '{app}' per owners.yaml")

    # Base version from the trusted checkout; head version as DATA via the API.
    base_spec = yaml_load(path)
    head_spec = yaml_load(fetch(f"{API}/repos/{REPO}/raw/{path}?ref={HEAD_SHA}").decode(), is_path=False)

    schema = json.loads(fetch(os.environ["SCHEMA_URL"]))
    schema_err = validate_subset(head_spec, schema)
    if schema_err:
        die(f"head spec fails the platform schema: {schema_err}")
    catalog = json.loads(fetch(os.environ["CATALOG_URL"]))

    fb, fh = flatten(base_spec), flatten(head_spec)
    changed = sorted(
        set(k for k in fb.keys() | fh.keys() if fb.get(k) != fh.get(k))
    )
    if not changed:
        print("gate: pass (no semantic change)")
        return

    tiers = catalog.get("quotaTiers", {})
    ceil = catalog.get("llmCeilings", {})

    def request_tier_ok(key: str) -> str | None:
        """None = allowed; a string = why it is operator-tier."""
        # homepage.* / pagesMeta.* are cosmetic self-serve (an owner decision).
        # `flatten` recurses dicts, so these arrive as `homepage.<leaf>` and
        # `pagesMeta.<slug>.<leaf>` keys. This must precede the `pages` branch
        # below, whose startswith("pages") would otherwise swallow `pagesMeta.*`.
        if key.startswith("homepage.") or key.startswith("pagesMeta."):
            return None
        if key == "quotaTier":
            return None if fh.get(key) in tiers else f"quotaTier '{fh.get(key)}' outside the ladder"
        if key == "hostname":
            v = fh.get(key)
            if v is None:
                return "hostname removal"
            return None if (v == app or str(v).startswith(f"{app}-")) else f"hostname '{v}' outside the app's own prefix"
        if key in ("llm.budget", "llm.rpm", "llm.parallel"):
            cap = ceil.get(key.split(".")[1])
            v = fh.get(key)
            return None if (isinstance(v, (int, float)) and cap is not None and v <= cap) else f"{key}={v} exceeds the fleet ceiling {cap}"
        if key in ("previews.ttl", "previews.quotaTier"):
            if key.endswith("quotaTier") and fh.get(key) not in tiers:
                return f"{key} '{fh.get(key)}' outside the ladder"
            return None
        if key.startswith("pages"):
            # additions under the app's own prefix only; removals are operator acts
            base_sites = set(base_spec.get("pages") or [])
            head_sites = set(head_spec.get("pages") or [])
            if base_sites - head_sites:
                return "pages site removal"
            bad = [s for s in head_sites - base_sites if not (s == app or s.startswith(f"{app}-"))]
            return f"pages site(s) outside the app's prefix: {bad}" if bad else None
        return f"'{key}' is a security-tier field"

    problems = [p for p in (request_tier_ok(k) for k in changed) if p]
    if problems:
        need_operator("; ".join(problems))

    print(f"gate: pass (request tier — {AUTHOR} self-serve on {app}: {', '.join(changed)})")

if __name__ == "__main__":
    main()
