# Security policy

The public `main` branch receives security fixes. No stable release series or support response time is promised yet.

## Sensitive reports

Do not post real tokens, customer data, panel credentials, hidden panel paths, complete subscription URLs, VLESS links, or unredacted logs in public issues. Use only synthetic data in public reproductions.

If the repository Security tab offers **Report a vulnerability**, use that private route. If it is unavailable, open an issue asking the maintainer for a private contact channel without disclosing the vulnerability, credentials, or customer data. Share sensitive details only after a private channel has been agreed. Use synthetic reproductions and minimal redacted evidence.

Useful reports describe the affected revision, component, preconditions, expected and actual behavior, and a minimal synthetic reproduction. Do not probe someone else's running service to reproduce an issue.

## Runtime boundaries

The application manages subscriptions, provider webhooks, administration sessions, and customer-related data. An operator must configure new credentials, restrict administration access, authenticate webhooks, and review their own deployment.

The inherited database initializer contains a default administrator credential, and the inherited entry point starts services and background jobs. Configure a new administrator password before exposing the panel. Use a fresh isolated database for development; never point a test or public demo at a production database.

Static checks and mocked tests cover selected failure modes. They do not establish the safety of a live deployment or verify all panel versions and network configurations. Changes to transport settings, existing subscription identities, and payment state require specific review.

## Accidental exposure

The relevant service owner should revoke or rotate exposed credentials. Removing a file or a visible string does not invalidate a credential or erase published history. Preserve only the minimum redacted evidence needed to investigate an incident.
