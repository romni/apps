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
import urllib.request

import yaml  # installed by the workflow step (pip)
import jsonschema

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
    owners_doc = yaml.safe_load(open("owners.yaml"))
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
    if status != "modified":
        need_operator(f"apps file {status} (new/deleted specs are operator acts)")
    app = m.group(1)
    if AUTHOR not in (app_owners.get(app) or []):
        need_operator(f"author '{AUTHOR}' does not own app '{app}' per owners.yaml")

    # Base version from the trusted checkout; head version as DATA via the API.
    base_spec = yaml.safe_load(open(path))
    head_spec = yaml.safe_load(fetch(f"{API}/repos/{REPO}/raw/{path}?ref={HEAD_SHA}"))

    schema = json.loads(fetch(os.environ["SCHEMA_URL"]))
    try:
        jsonschema.validate(head_spec, schema)
    except jsonschema.ValidationError as e:
        die(f"head spec fails the platform schema: {e.message}")
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
