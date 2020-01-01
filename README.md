# apps — the fleet registry

This repository is the **fleet registry**: the single, version-controlled source
of truth for which apps the homelab runs and what each is allowed to do. It holds
**grants as data** — one declarative `AppSpec` per app — reconciled onto the
cluster by a batch apply of the platform's `fleet` Pulumi stack. There are no
controllers and no webhooks in the request path.

## Layout

```
apps/<name>.yaml     one AppSpec per app (the grant)
owners.yaml          app -> bot user(s) allowed request-tier self-merge (the trust root)
.forgejo/workflows/  the tiered merge gate
```

An **AppSpec** (`apiVersion: fleet/v1`) declares an app's surface: its repo,
namespace, quota tier, egress grants, pages sites, preview sandbox, and more. The
authoritative shape is the JSON Schema `schema/appspec.json` in the platform repo
(`homelab/rlyeh`); the fleet factory (`infra/lib/fleet.ts` there) is the
executable validator. Capability *values* — the egress grant names, auth modes,
and quota-tier constants — are defined in the platform's catalog
(`infra/lib/fleet-catalog.ts`). A new capability is one catalog entry there; it
never requires a change in this repo.

## How a change flows

```
edit apps/<name>.yaml  ->  open a PR  ->  the gate runs (base-ref)  ->  merge  ->  fleet apply converges
```

The gate runs from the **base ref** (`pull_request_target`), so a PR cannot alter
the gate that judges it. It fetches `schema/appspec.json` from `homelab/rlyeh@main`
(raw) and cross-checks catalog membership, then applies the tiered policy below.

## Tiers (mechanically enforced by the gate)

**Request-tier** (the app's own bot user, per `owners.yaml`, may self-merge) — a
PR that touches *only* `apps/<own-app>.yaml` and changes *only* request-tier
fields within schema bounds:

- quota-tier moves within the S / M / L ladder,
- extra hostnames under the app's own `<name>*` prefix,
- cronjob / job counts within cap,
- LLM budget / rpm / parallel raises within the fleet-wide ceiling.

**Operator-tier** (an operator's approving review is required) — everything else:

- a new file (app birth) or a deletion (app death),
- `owners.yaml`, the schema reference, or any workflow,
- security-tier fields: `egress` classes, DB roles, `sso` / auth changes,
  `previews.enabled` (it creates a namespace + identity binding).

All caps are platform constants (owned in `homelab/rlyeh`) — raising a *cap* is
always an operator act.

## Evolution

The schema evolves **additively**: new fields arrive optional and defaulted;
removal is deprecate-then-error over two releases. Merged specs are never
invalidated in either direction.

## Reconciliation & DR

`task up-fleet` (in `homelab/rlyeh`) pulls this registry clone and applies the
fleet stack. A registry CI apply job is the routine converge path; `task up-fleet`
is the operator's manual / recovery lever. This repo is mirrored to GitHub like
the platform repo, so disaster recovery restores it *before* Forgejo exists —
the platform bootstraps with zero app knowledge, then the registry re-stamps
every sandbox.
