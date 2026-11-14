# Security operations

## Historical Google API credential (rotation required)

A Google API credential was committed in `main.py:17` at historical commit
`1a7a984502c1178164ac12290eb44066d0999358`. Removing it from HEAD does not revoke it.
Do not paste that value into issues, logs, tests or tools to check whether it works.

The Google Cloud project owner must revoke that credential, review its usage and
billing since exposure, issue a replacement restricted to the required API and
deployment, and update the deployment secret. Record the revocation time and
provider key identifier (never the key value) in the incident record. Confirm old
deployments and forks no longer reference it. History cleanup can follow revocation
but cannot substitute for it. Provider revocation is **pending external confirmation**.

Use repository/provider secret scanning and push protection. Review future commits
for credential literals; only environment variables should hold runtime secrets.

## Health and cache semantics

`GET /health` performs no model request. `gemini: configured` means only that a
client is configured, not that credentials were remotely validated. Shared analysis
cache entries contain caption/frame extraction only. Coordinates and branch
selection are resolved for each request; never cache a GPS-dependent final response
by URL alone.

Offline regression checks: `python -m pytest tests -q`.
