# PEPEL VPN Bot

PEPEL is a Telegram subscription-management application with a Flask administration interface, SQLite storage, and integrations for 3x-ui and Blitz/Hysteria-based VPN services. It coordinates payment confirmation, subscription issuance and renewal, retries, and customer delivery.

**Status: early public source release.** This source export is available for independent installation and review. The source comes from a working private service; that is evidence of internal use, not evidence of external adoption of this candidate.

Subscription identity handling was hardened during preparation and is covered
by regression tests. Read [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for compatibility
limits, recovery behavior and the scope of offline validation.

## Community purpose and service usage

PEPEL VPN aims to help people in border regions stay in touch with loved ones
and access information amid recurrent shelling and internet restrictions
imposed by Roskomnadzor. The open-source code supports the service's
subscription management and maintenance.

As of 2026-10-28, the service maintainer reports **approximately 2,000 active
VPN service users**. This is an approximate, maintainer-reported service
figure. Its reporting period and counting method have not yet been documented.
Independent installations of the open-source software have not yet been
measured.

Evidence available for review includes the public source, GPLv3 attribution,
[passing offline CI](https://github.com/rijiiokun-wq/pepel-vpn-bot/actions/runs/40488110304),
and the regression tests. These demonstrate the software's maintenance and
test coverage; they do not independently verify the service-user estimate.
Only aggregated usage figures will be published.

If you independently install this software, you can help document its usefulness
by opening a GitHub issue describing your setup, a successful installation, or a
reproducible problem. Share your experience only with information you consent
to make public, and use synthetic or redacted examples without customer data,
credentials or complete subscription links.

## What is included

- Telegram purchase, renewal, trial, referral, and support flows.
- Payment-provider integrations and webhook processing with idempotency and recovery paths.
- Python adapters for creating and updating client records in supported VPN panels.
- Retry, host-health, delivery-monitoring, and startup-recovery logic.
- Flask administration routes and templates, including scoped panel access.
- SQLite schema initialization and migrations, offline regression tests, and maintenance checks.

The application supports `main` and `reserve` bot runtimes with separate credentials and shared storage. That is the original service topology; an independent installation still needs its own configuration and an explicitly tested setup.

The Python implementation performs issuance. The experimental Go backend from the private repository is omitted because its real XUI issuance implementation is still a stub.

The active implementations are `shop_bot.webhook_server.app` and
`shop_bot.modules.xui_api`. Unreferenced legacy copies at `shop_bot.app` and
`shop_bot.xui_api` are omitted from this export.

## Prepare dependencies and run the offline checks

Use Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Run these commands from the candidate directory:

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev python tools/run_offline_tests.py
uv run --frozen --extra dev python scripts/lint_async_safety.py .
uv run --frozen --extra dev python scripts/lint_3xui_guardrails.py .
```

Dependency installation needs internet access. The current `uv` project is a source checkout with a virtual root: `uv sync` installs dependencies, while the test runner adds the checkout's `src` directory to Python's import path. This quickstart does not install a standalone `vless-shopbot` distribution.

The offline test runner creates temporary test storage, starts pytest with a clean environment, and blocks Python IPv4/IPv6 socket connections and DNS calls. It is not an operating-system sandbox: native code and subprocess networking are outside that block. Do not supply service credentials or a production database. Tests use mocks and synthetic fixtures for external systems; they do not validate a live VPN deployment.

The runner returns pytest's actual exit code. It does not accept known failures as success. The inherited failure baseline is reference material for understanding the source snapshot, not a waiver for candidate failures.

Validation on 2026-10-27: **903 tests passed, with no failures or skips**, using
CPython 3.11.16 and the offline runner. Ruff `F,E9`, async-safety and 3x-ui
leakage checks passed. Locked dependency installation was checked on Python
3.11 and 3.12. The application's tests were retained and refreshed; tests for
omitted private deployment/repair scripts and obsolete baseline tooling were
excluded. This result does not validate live providers or panels.

The static checks detect selected patterns; a successful scan does not prove that all secrets, blocking calls, or security issues have been found.

## Preview the administration UI locally

```sh
uv run --frozen --extra dev python tools/preview_admin.py
```

Choose a temporary password when prompted, then open
`http://127.0.0.1:18084/login` and sign in as `preview`. The helper creates a
fresh temporary database, starts no bot or scheduled jobs, and allows only
selected read-only pages plus login/logout. Changes, payment endpoints and bot
startup requests are rejected. Ctrl+C stops the preview and removes its data.
The preview blocks outgoing Python sockets and DNS; it is not an OS sandbox for
native libraries or subprocesses. It does not load a repository `.env` file.

## Runtime setup remains a separate step

Do not use `python -m shop_bot` as an offline smoke test. The application entry point starts the web server, attempts to start the Telegram bot, and schedules background jobs. It can contact configured providers and panels.

An independent runtime needs, at minimum:

1. A fresh local SQLite database selected by `SHOP_DB_FILE` and a clear backup policy.
2. A new panel administrator password and Flask session secret, configured before granting access to the panel. The inherited database initializer includes a default administrator credential; it must not be exposed.
3. Your own Telegram bot and administrator configuration. Some settings are stored in SQLite; providing environment variables alone does not populate every setting. Bootstrap username discovery may call Telegram.
4. Your own panel endpoints and credentials, followed by compatibility checks for the panel version, selected inbounds, TLS, and subscription behavior.
5. Explicitly enabled payment providers, webhook authentication, and a separately reviewed HTTPS deployment.

The inherited entry point binds its Flask development server to all interfaces. A public deployment must supply an appropriate production web-server and network configuration. No deployment setup is implied by the offline quickstart.

The public candidate excludes the private service's databases, environment files, access keys, customer data, production Compose/nginx configuration, deployment workflows, and operational incident history. Create configuration for your own installation; do not reuse the original service's infrastructure.

## Integration invariants

Normal issuance and renewal should modify client records and client-to-inbound bindings. They must preserve inbound transports, paths, proxy settings, and existing subscription identities. Do not delete or recreate an inbound to add a client.

Panel repair and migrations require a separate reviewed procedure. New nodes must remain ineligible until connectivity and compatibility checks pass. The project's retry and payment-idempotency paths are part of correctness, not optional cosmetic behavior.

Product policies and quotas in this export reflect PEPEL's source snapshot. Review them before offering subscriptions under a different service. A copied default is not a promise about your installation's behavior.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes and [SECURITY.md](SECURITY.md) before reporting a sensitive issue. Use GitHub issues for non-sensitive reports and the private reporting route in SECURITY.md for vulnerabilities. No support response time or external adoption figures are claimed.

## License and attribution

This candidate is distributed under the GNU General Public License version 3; see [LICENSE](LICENSE). PEPEL is derived from [evansvl/vless-shopbot](https://github.com/evansvl/vless-shopbot). Preserve the upstream notices and the notices for subsequent changes when redistributing it.

Bundled Chart.js retains its separate MIT notice in [CHART-LICENSE.md](src/shop_bot/webhook_server/static/vendor/CHART-LICENSE.md). Additional bundled-component and upstream asset notices are in [NOTICE.md](NOTICE.md). Other dependencies retain their own licenses.
