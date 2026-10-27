# Contributor guardrails

This is the PEPEL VPN public-source candidate, derived from VLESS Shop Bot.
Preserve GPLv3 attribution and bundled third-party license notices.

## Subscription and panel changes

- Normal issuance, renewal and synchronization use client operations only.
  Do not update an entire inbound, recreate an inbound, or change transport,
  proxy, CDN, path, port or security settings as a client-API fallback.
- Read and validate all relevant client identities before the first mutation.
  Missing, malformed, ambiguous or conflicting identity information must fail
  closed. This preflight is not a distributed transaction; later failures still
  require explicit retry/recovery handling.
- Preserve the public logical subscription ID and existing client UUIDs and
  component subscription IDs. A centralized panel may use distinct physical
  IDs for different components of the same logical subscription. Do not rotate
  those IDs on renewal. Generate an ID only for a confirmed new component.
- Legacy collisions or bindings requiring an identity change need a separate
  explicit repair or migration with a dry run, before/after diff, backup,
  rollback procedure and post-checks. Normal issuance must not silently detach
  conflicting legacy bindings.
- Preserve unknown and transport-specific fields. Never copy Reality/TCP flow
  into WS/XHTTP clients that do not support it.
- New nodes remain ineligible until panel connectivity, TLS policy, Xray,
  enabled inbounds and subscription compatibility have been checked.
- Paid Standard and Super use the published nodes, with WHITELIST limits of
  5 GiB and 40 GiB respectively and reset interval 30. Super trials use 2 GiB;
  legacy ordinary-only Standard trials and explicit unlimited overrides remain
  separate. Do not impose WHITELIST limits on ordinary unlimited nodes.

## Application changes

- Keep payment processing idempotent. A successful payment must result in
  issuance, a durable retry with a reason, or an actionable critical alert.
- Preserve startup catch-up, stale-transaction recovery and runtime isolation.
- Do not perform blocking network or database operations directly in async
  handlers. Use the existing executor/thread wrappers.
- Use synthetic test data. Never put live tokens, panel paths, user identities,
  subscription URLs, private databases or credentials into source or logs.
- Keep the disposable preview independent of live bots and payment providers.

## Verification

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev python tools/run_offline_tests.py
uv run --frozen --extra dev ruff check src scripts tools tests --select F,E9
uv run --frozen --extra dev python scripts/lint_async_safety.py .
uv run --frozen --extra dev python scripts/lint_3xui_guardrails.py .
```

The offline runner uses temporary storage and Python socket guards. It is not
an OS sandbox. Do not give it production credentials or state. Do not suppress
test failures through a known-failure baseline. Changes to
issuance need meaningful identity, failure and transport-preservation checks.
