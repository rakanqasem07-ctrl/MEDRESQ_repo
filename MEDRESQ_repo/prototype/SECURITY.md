# MEDRESQ security notes (prototype)

All data is synthetic. This is a competition prototype, not a production system. Review below is by code reading and automated tests (46 tests, 14 of them security tests). No external scanner (bandit, pip-audit, semgrep) could be installed in the build environment, so none was run.

## What is protected
- Sign-in: each account has its own scrypt-hashed password (accounts.json). First run creates random passwords in PASSWORDS.txt. `MEDRESQ_EASY_PASSWORDS=1` sets a shared "demo" password: for tests and a live demo only.
- Sessions: random token in an HttpOnly, SameSite=Strict cookie (Secure on https). Expire after 1 hour idle or 12 hours total.
- Brute force: 5 wrong attempts per user and address in 5 minutes gives HTTP 429.
- Roles enforced on the server: manager (own center only), coordinator (decisions, settings, rules, reset, pitch), reviewer (read only). The decision role comes from the session, never from the request body.
- Requests: POST must be JSON, Origin must match (blocks cross-site posts), body limit 1.5 MB, numeric and text inputs validated, errors return 400 or a generic 500 with no stack trace.
- Browser hardening: Content-Security-Policy, X-Content-Type-Options, X-Frame-Options DENY, Referrer-Policy, Permissions-Policy, no-store on API and report responses.
- Injection: all user text shown with innerHTML goes through `esc()`; CSV exports neutralise formula cells; uploaded CSV text is length-limited and sanitised.
- Safety rules enforced server-side: controlled medicines are never proposed or requestable; cold-chain medicines need the sender's confirmation; donors must keep 14 days of stock.

## Known limits
- CSP still allows inline scripts (single-file app). Moving scripts out would tighten it.
- Over `--phone` (LAN) the connection is plain HTTP: use only a trusted Wi-Fi. Use HTTPS (reverse proxy) for anything beyond a demo.
- Accounts are a demo identity system: no password reset, no MFA, no per-user audit of logins.
- The PWA install and notification service worker work only on https or localhost.
- PASSWORDS.txt holds first-run passwords in clear text: delete after handing them out.
- Impact page figures are typed by the user and stored in that browser only.
