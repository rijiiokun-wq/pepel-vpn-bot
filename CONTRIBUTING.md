# Contributing

This is a local public-release candidate. The public repository and maintainer support channels have not been launched yet.

PEPEL is derived from `evansvl/vless-shopbot` and uses the GNU GPL version 3. Preserve applicable copyright notices and license terms. Contribute only code and assets you have the right to distribute under compatible terms.

## Development setup

Use Python 3.11 or later and `uv`:

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev python tools/run_offline_tests.py
uv run --frozen --extra dev python scripts/lint_async_safety.py .
uv run --frozen --extra dev python scripts/lint_3xui_guardrails.py .
```

Installation needs network access to obtain dependencies. The runner uses a clean child environment, temporary test storage, and a Python socket/DNS block; it does not isolate native code or subprocess networking. Tests should mock external APIs. Do not supply production environment variables, databases, tokens, or customer exports. Do not start the bot to run tests. The runner returns pytest's real exit code, including failures already present in the source snapshot.

## Scope and review

Start a change with a concrete problem and an example of the intended behavior. Keep changes focused and include a relevant regression test when behavior or a security boundary changes. Explain test failures honestly; do not expand a failure baseline just to make a change appear green.

The private source snapshot contained known test failures. The public candidate must report its own current results and distinguish remaining failures from regressions. An unchanged baseline is not equivalent to a clean test run.

For issuance, renewal, node synchronization, and subscriptions:

- Preserve inbound transport settings, CDN/proxy paths, and unknown panel fields.
- Change only the intended client records or bindings during normal operations.
- Keep existing subscription identities stable unless an explicit migration is being implemented.
- Preserve payment idempotency, durable retry state, and actionable error handling.
- Keep blocking network and database calls off the asyncio event loop.

Describe any migration, validation, and rollback requirements in the proposed change. Production deployment and panel repair are separate operations and must not be hidden inside a test or ordinary issuance path.

## Test data and diagnostics

Use `example.com`, documentation IP ranges, temporary databases, and invented identifiers. Do not reuse real customer identifiers, tokens, complete subscription/VLESS links, panel paths, or screenshots with credentials. Splitting a real secret into string fragments does not make it a safe fixture.

Diagnostics should identify the file, rule, operation, and reason without exposing sensitive values. Report security issues using the process in [SECURITY.md](SECURITY.md).

## Before the first public release

The maintainer must publish the actual contribution and private reporting channels, finish the asset/configuration review, document a reproducible independent runtime setup, and report remaining unsupported scenarios. These preparation requirements do not imply that a public release has already happened.
