# Pooled native credentials

Run the credential service with the same Omnigent wheel as the server. The API server must have access to its Unix socket. The runner host must have access to the same encrypted profile volume. Restrict both mounts to the Studio service identity. The host receives only the selected profile directory name.

```sh
python -m omnigent.studio.credential_agent
```

| Environment variable | Consumer and value |
| --- | --- |
| `OMNIGENT_STUDIO_SEAT_SOCKET` | Server and service. Absolute socket path in a private directory. |
| `OMNIGENT_STUDIO_SEAT_ROOT` | Credential service and pooled native host. Absolute private directory containing one 0700 directory per provisioned seat. |
| `OMNIGENT_STUDIO_SEAT_MANIFEST` | Credential service. Absolute path to its private JSON manifest. |
| `OMNIGENT_STUDIO_API_FALLBACK` | Server. Set to `1` to allocate verified prepaid grants for captured PR reviews after subscription exhaustion. |
| `OMNIGENT_STUDIO_LOCAL_FALLBACK` | Server. Set to `1` to select a configured Sparkle or vLLM profile after exhaustion. |
| `OMNIGENT_STUDIO_DISPATCH_ACCOUNT` | Server. Registered Studio bot account for leased free-agent sessions. |
| `OMNIGENT_STUDIO_DISPATCH_TOKEN_FILE` | Server. Absolute private file containing this account’s Omnigent access token. |
| `OMNIGENT_STUDIO_PREVIEW_REPOSITORY`, `OMNIGENT_STUDIO_PREVIEW_HOST` | Server. GitHub owner/repository and trusted VPN hostname for K1 workspace checkout receipts. K1’s configured NodePort range is 32100–32199. |
| `OMNIGENT_STUDIO_GITHUB_READ_TOKEN` | Server. Read access to trusted preview workflow runs and their artifacts. |

The manifest's `workspaces` object is keyed by Omnigent workspace ID. Each entry contains the following configuration.

* `subscriptions` is keyed by the SHA-256 hex digest of the admin-configured profile reference. Each entry specifies `harness`, opaque `profile` directory name, HTTPS `quota_url`, and private `quota_token_file`. The quota read accepts HTTP 429 with optional numeric Retry-After, or JSON with boolean `available`. The quota service must observe the particular subscription identified by its token and endpoint. An unavailable quota monitor cannot attest seat readiness.
* `on_prem` is keyed by native harness. Each entry has `profile` and `provider` (sparkle or vllm). Provision the private native profile with the local inference URL, model and the credential required by that endpoint.
* `api` is keyed by native harness. Each entry specifies `model`, inference `origin`, HTTPS `grant_url`, private bearer `issuer_key_file`, and private PEM `attestation_public_key_file`. Enable this mode only with an issuer that can enforce prepaid, non-overdraft balances on each isolated credential. Deployments without such an issuer can operate subscription and on-prem profiles.

The issuer receives a POST with workspace_id, lease_id, pull_request, session_id, harness, model, origin, hard_limit_cents, no_overdraft=true, billing=prepaid, and Unix expires_by. It must return a distinct credential and an EdDSA-signed JWT in the attestation field. The JWT must repeat the request bindings, add the lowercase SHA-256 digest of the credential in credential_sha256, identify the grant URL as issuer and omnigent-studio-seat as audience, and include iat, nbf and exp. The expiry must fall within 20 minutes and the current UTC billing month. The issuer must expire the credential at that time. The broker removes its local auth/config files at expiry. The service reserves cents in SQL before requesting a grant and retains the reservation if the reply is lost.

The host sets CODEX_HOME or CLAUDE_CONFIG_DIR before starting its native process. The local 429 proof measures subscription admission through the outgoing host launch frame. The server cools a throttled seat and tries another before delivering that frame. A later error from a running native turn stays on the original session because its tools may already have run.

An administrator can switch an individual seat between subscription and fallback routing. A fallback seat participates in the same weighted dispatch selection and uses the server-enabled tiers in order. Disabling a seat removes it from selection. Updating its weight, routing mode or enabled state preserves its existing credential reference. Rotating that reference or changing its harness requires an explicit replacement reference.

Fleet inventory returns the server-derived seat administration capability. Administrators see seven-day usage alongside each seat, counting reserved, admitted and uncertain attempts. Provider capacity comes from the selected bot-review launcher's organization ledger. Missing or invalid capacity returns null.

## Free-agent round evidence

The lease worker watches the bot-owned native session. It binds each result to the frozen task, round prompt, completed assistant turn and same-turn shell receipt. A failed local check can schedule a follow-up in the same session. The third failure stops the session and writes one Inbox escalation. Missing prerequisites or ambiguous evidence close the lease as unknown. An ambiguous message delivery is never replayed. Manually recorded rounds are available for standalone claims and carry separate provenance.

The wheel installs `omni-studio-verify-round`. It checks a clean KVARK commit against the source mounted under `/opt/studio/sources/KVARK`, runs the existing `org dev test-int` command with a reduced environment, and reports Git revisions plus the output digest. Fleet treats this as a local observation. To record a verified pass, the server reads the existing KVARK default-branch Nightly Integration workflow using `OMNIGENT_STUDIO_GITHUB_READ_TOKEN`. The job, integration step, run attempt and task-linked commit must match. The receipt includes the GitHub job URL. Locally authored commits without matching CI evidence remain unverified.
