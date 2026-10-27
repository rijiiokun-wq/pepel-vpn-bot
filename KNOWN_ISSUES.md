# Compatibility and validation scope

This is a local review candidate. It has not been published or deployed. CI
uses pytest's actual exit status; the private known-failure baseline and its
acceptance wrapper are not included.

## Subscription identity handling

Issuance now reads and validates all relevant bindings before its first client
mutation. Existing client UUIDs, public subscription IDs and component IDs are
preserved. An unreadable or malformed snapshot, conflicting identity or legacy
binding that needs migration causes an explicit refusal before any write.
Identifiers are generated only for confirmed new components.

A centralized panel may represent secondary bindings using distinct physical
subscription IDs. The public logical subscription remains stable. Existing
legacy collisions require a separately reviewed repair or migration; ordinary
issuance no longer silently detaches these bindings.

This is a preflight guarantee, not a distributed transaction. A network error
after a successful write can leave partial new state. Normal retries use the
verified existing identities; the application no longer guesses which clients
to delete after an uncertain result or timeout. An unresolved partial state may
need explicit repair.

## Runtime onboarding and live validation

The disposable UI preview is the supported local demonstration. The inherited
application entry point and administrator defaults are not a production setup
guide. A live installer must initialize its own administrator and session
secret, configure providers explicitly, and use an appropriate HTTPS server.

Offline tests use mocked providers and panel responses. They do not establish
compatibility with every live panel version or verify payment delivery on a
running installation. The private service's deployment workflows and access
configuration are absent from this candidate. No live deployment was performed
as part of this preparation.

## Changes made during preparation

- Leak-checker diagnostics no longer print matched sensitive values.
- Test fixtures use synthetic infrastructure and identities, preserving their
  intended equality and case-normalization relationships.
- Enabled Blitz issuance is allowed through its intended branch; disabled
  Blitz still refuses issuance.
- Normal VLESS/Hysteria client upsert refuses unsupported client API operations
  instead of attempting an automatic full inbound update.
- Subscription identity preflight, stable IDs and refusal of implicit cleanup
  are covered by dedicated regression cases.
- The read-only UI preview uses temporary storage and a user-chosen password,
  without starting bots or scheduled jobs.

These changes exist only in this candidate. They have not been deployed to the
private service.
