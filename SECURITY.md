# Security operations

## Historical Google API credential (invalidity confirmed)

A Google API credential was committed in `main.py:17` at historical commit
`1a7a984502c1178164ac12290eb44066d0999358`. Removing it from HEAD does not revoke it.
Never paste that value into issues, logs or tests.

On 2026-11-17, the incident response checked the exact historical credential
against Google's non-billable Gemini models-list endpoint. Google rejected it
with HTTP 400 and `API_KEY_INVALID`; the project key list also returned no active
keys. This confirms the exposed credential was invalid at the check time. The
response agent did not revoke it, and the revocation date/cause is not established.

The Google Cloud project owner should still review usage and billing since
exposure, confirm deployed secrets and forks no longer reference it, and use a
replacement restricted to the required API and deployment if service is needed.
Record incident/provider identifiers without credential values. History cleanup
cannot substitute for invalidating exposed credentials.

Use repository/provider secret scanning and push protection. Review future commits
for credential literals; only environment variables should hold runtime secrets.

## Health and cache semantics

`GET /health` performs no model request. `gemini: configured` means only that a
client is configured, not that credentials were remotely validated. Shared analysis
cache entries contain caption/frame extraction only. Coordinates and branch
selection are resolved for each request; never cache a GPS-dependent final response
by URL alone.

Offline regression checks: `python -m pytest tests -q`.
