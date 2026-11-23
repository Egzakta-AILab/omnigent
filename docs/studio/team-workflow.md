# Working together in Egzakta Studio

Updated after the third design pass, 23 November 2026 ([KVAD-1571 decisions D41 to D52](https://linear.app/kvark/issue/KVAD-1571/dev-studio-on-omnigent-agentic-pr-review-and-triage-bot-on-the-rack#comment-2dbdfa30)).

A developer closes their laptop while an agent tests a KVARK change on the rack. An hour later, QA opens the task. They need four facts on the first screen: what the author wanted to change, which build they can try, what the agent observed, and the single question that needs an answer. Today the fork starts an owner-private session from Linear and saves private reviews. The missing link is the shared task anchor and managed rack fleet connecting those two moments. [Linear pilot](../../examples/extensions/linear-board/README.md) · [Studio pilot](README.md)

The baseline explains the priority. In the board's sample of 100 merged KVARK PRs (measured 13 November 2026), 60 changed more than 500 lines and took a median 53.4 hours to merge. The other 40 took 2.8 hours. Giving a second person the changed behavior, a pinned build to try, and commit-bound evidence tests a bottleneck the team can directly measure. [Board research](http://192.168.36.11:30708/r/e4bbcfa1-fc8b-4949-9611-1ce29919eddf?d=ppage)

## Shared task anchor (reopened D32)

Linear remains the system of record for issues, assignees, cycles, priorities, and releases. Studio adds a thin `studio_tasks` table keyed by `(linear_workspace, linear_issue_id)` visible to authenticated Egzakta users on the rack instance. It holds the acceptance check, linked PRs across repositories, active sessions, pinned previews, and open handoff cards.

Personal transcripts stay governed by `SqlSessionPermission`. Two teammates opening the same Linear issue land on the same shared task rather than two separate owner-private projects, while opening a colleague's transcript, private review, or shell still requires an explicit grant. Linear stays read-only plus one idempotent bot verdict comment by default; status changes, handoff reassignment, and failed QA issue creation require explicit human confirmation (D47).

## One change through Studio

The laptop pilot verified the two ends separately: reopening a session from Linear and preserving a private PR review across restarts. The immediate 1-week engineering gate connects two people on one real multi-revision PR, with the second person acting offline in under 15 minutes and zero out-of-band clarifications. [Laptop pilot guide](README.md)

| Moment | What the team sees and does | What the next person can rely on |
| --- | --- | --- |
| Pick up an issue | In `egzakta.work`, choose the Linear issue, confirm repositories, write one observable acceptance check, and pick a laptop or rack host with any of the five native harnesses (Claude Code, Codex, Cursor, Pi, OpenCode). | One shared task URL, owner, concrete acceptance check, and linked session. |
| Work together | Dispatch a free rack agent or hand off a question to a teammate. The recipient gets an Inbox decision card (`Accept`, `Not me`, `Away`, `Reply`) with a structured `studio-handoff@1` packet. | The exact question, linked evidence, who owes an answer, and the recorded decision. |
| Check a PR | Rack Actions triggers a deterministic base-branch review at `head_sha` (no model-chosen roster pause, D17/D49). `egzakta.review` shows unmerged 5-lens findings beside an agent-generated layered walkthrough with diff-line citations and read-only `Services` config context. | Repository, `base_sha` and `head_sha`, check command, `check_failed` vs `env_blocked` vs `unverified` status, and artifact hash. A new push marks the prior verdict stale. |
| Ask for judgment | Deterministic base-branch rules assign `Auto`, `Human review`, or `Vibe check` and pick one non-author reviewer from weekly profiles. Priority caps at P1 without external Linear urgency or blocked-PR count. | The asked reviewer inspects the walkthrough or pinned preview, runs Q&A against the frozen verdict on read-only chat or their own host (0 non-admin shells on `bot-review`), or dismisses `should fix` with a logged reason (`blocks merge` requires a lead). |
| Release | In `egzakta.release`, QA opens the exact KVARK candidate (with the deployed `Services` template/chart revision shown as read-only context), reviews non-blocking agent pre-checks across the 88 scenarios, and records sign-off. | Candidate tag, `Services` revision context, 88/88 commit-pinned scenario results with media/log evidence, and sign-off checked before staging/production promotion. |

### A concrete cross-repository example

[KVARK#1538](https://github.com/Egzakta-AILab/KVARK/pull/1538) (merged 17 November) adds a manually dispatched per-PR Coder workspace workflow and cleanup. [Services#140](https://github.com/Egzakta-AILab/Services/pull/140) adds the Coder server, Terraform template, Calico network policy, and Omnigent server deployment. Both belong to [KVAD-1115](https://linear.app/kvark/issue/KVAD-1115). Before shipping on the rack, `Services#140` drops its duplicate preview script and restores `.github/workflows/testing.yml`, while `KVARK` keeps the single `deployment/coder/pr-preview-start.sh` updated to check out an explicit `head_sha` rather than mutable `pull/$pr/head` (D42).

The shared `KVAD-1115` task shows both PRs side by side. When QA runs a preview check, Studio records both the requested commit SHA and the commit actually checked out, plus the `Services` template version. If the preview fails to boot or a dependency is missing, Studio records `env_blocked` and routes it to DevOps while leaving the product verdict open. When a preview expires, archived screenshots and check receipts stay readable on the task.

## Architecture: thin fork seams, Coder provider, and five extensions

Omnigent V1 browser extensions run in sandboxed iframes over a permission-checked `MessageChannel` RPC bridge and cannot add server routes or database tables ([Extension contract](../extending/extensions.md)). To keep weekly upstream rebases small (R1), the fork rebases directly onto upstream `v0.23.0` (inheriting session grids, shared read-only automations with MCP scoping, PR diff-since-commit, inline comments as chat context, and Codex reconnect fixes; verifying one native review and reconnect under the `v0.23.0` `sys_os_*` native-harness change) and exposes only thin server seams (`studio_tasks`, review/proof/dispute tables, deterministic triage, dispatch-time seat pool, connectors, and one versioned `studio.*` RPC schema).

All five repo-agnostic UI extensions plus the Coder sandbox provider live in one separate private repository, `Egzakta-AILab/studio-extensions`, pinned in the fork's Docker build (D46). Repo-specific differences (`KVARK`, `Services`, `kvark-sparkle`, and future client repos) live in per-repo Product Packs (`studio.yaml`, `review-profiles.yaml`, `verify` skills, `graph.json`, QA scenarios).

| Package | Seam | Responsibility |
| --- | --- | --- |
| `egzakta.work` | `omnigent.extensions` | Linear board, shared `studio_tasks` anchor, acceptance check, multi-repo PR links, pre-start confirmation, `studio-handoff@1`, explicit Linear write-back. |
| `egzakta.fleet` | `omnigent.extensions` | Managed hosts, Coder pools (`1 + 3` cap), leased free-agent dispatch, pinned previews, and **Admin -> Seat Pool** load balancer and failover controls. |
| `egzakta.comms` | `omnigent.extensions` | Unified Inbox decision cards, `@` people mentions, finding threads, Team view (who is on what, waiting on whom, stale PRs), away stand-ins. |
| `egzakta.review` | `omnigent.extensions` + `review.panel` | Deterministic 3-lane triage, Cycle 22 agent-generated layered PR walkthrough + read-only `Services` context (plus static `graph.json` footprints in Package K), unmerged 5-lens proofs, dispute/dismissal, App-committed fix patches. |
| `egzakta.release` | `omnigent.extensions` | Linear + GitHub release cockpit (KVARK first, `Services` revision read-only), 88 QA scenarios with shared authoring and QA sign-off, non-blocking agent pre-checks, promotion gate. |
| `egzakta.coder` | `omnigent.sandbox_providers` | Coder Community workspace start, wake, hibernate, and teardown behind internal VPN DNS with TLS, retiring device-grant polling for managed pools. |

### Free agents, Coder workspaces, and the work-subscription seat pool

Omnigent is the sole agent loop across all five native harnesses (`claude-native`, `codex-native`, `cursor-native`, `pi-native`, `opencode-native`); Coder Agents does not run beside it (D45). Coder Community supplies the workspace substrate via `egzakta.coder` with split ownership (D42):
- `dev-personal`: developer's own Coder token, own GitHub external auth, and personal Claude/Codex/Cursor/Pi/OpenCode logins.
- `bot-review` and pooled free agents: owned by `studio-bot`, hard-capped at `1` always-on `bot-review` (with MLflow enabled in `org dev infra`) plus up to `3` concurrent full-stack workspaces (`queued_for_capacity` overflow).
- `pr-preview`: on-demand pinned-SHA workspace under `studio-bot`, where **Open preview**, **Join session**, and **Open shell** each check an independent permission.

Work Claude Code (Opus 5.5) and Codex subscriptions are pooled on the server behind a dispatch-time load balancer using isolated, encrypted `CLAUDE_CONFIG_DIR` and `CODEX_HOME` profiles per seat, alongside org API-key (`<= $3/PR`, `$250/mo` cap) and on-prem `vLLM` / `Sparkle` slots (D12). Dispatch chooses the lowest-usage eligible slot outside cooldown; a 429 or quota error triggers cooldown and failover in under 10 seconds. Pooled runner hosts allow zero non-admin shell grants so seat credentials stay isolated (D26), and every slot flips to an API key or `Sparkle` with one admin toggle in `egzakta.fleet` while Q1 (vendor Commercial Terms) remains tracked.

Free-agent dispatches use idempotent 20-minute heartbeat leases (a stale lease moves to `unknown` rather than silently relaunching), run repo `verify` skills through `org dev`, stop after 3 failed fix rounds to escalate to the requester's Inbox with evidence, support self-scheduled follow-ups, and finish with a draft PR or patch plus a `studio-handoff@1` card (D48).

### Inbox first, Discord deferred (D40)

Omnigent Inbox remains the sole notification channel while the core workflow rolls out. Every Inbox request carries a stable deduplicated request ID so that when Discord is enabled later (after Cycle 23 or if Inbox miss telemetry warrants it), Studio can deliver opt-in outbound nudges linking back to the authenticated request without posting private review text in Discord.

## Rollout gates and verification commands (D52)

1. **Immediate 1-week engineering gate (G1a):** Offline two-person handoff on one real multi-revision PR. Given only the shared task link with the author offline, the reviewer identifies the tested commit, opens the evidence/preview, and answers the Inbox request in `< 15 min` with `0` out-of-band clarifications (`verify_studio.py` + pilot receipt). Followed by 15/15 rack onboarding (G1b).
2. **Fleet and seat-pool health (G5):** `<= 3 min` cold start / `<= 30 s` wake, `1 + 3` concurrency cap, `env_blocked < 10%`, `0` silent stalled leases, `< 5%` pool-exhausted or throttled dispatches, `< 10 s` slot failover, `0` non-admin shell grants on pooled hosts (`verify_studio.py fleet --window 7d`).
3. **Shadow review and auto-merge authority (G2, G3, G4):** Score raw finding recall, verified finding recall, and final agreement separately across 30 real KVARK PRs. Open auto-merge only at `>= 27/30` agreement, `0` missed blocking defects, and `< 5%` throttled reviews (`verify_studio.py shadow-report --window 7d`). Keep auto-merge open only with `0` reverts in the first 30 auto-merges. Product target: KVARK PRs `> 500` lines `< 24 h` median and first verdict `<= 30 min` on 90% of PRs.
4. **QA release traceability (G6):** `88/88` active KVARK release scenarios recorded in Studio with commit-pinned evidence before promotion (`verify_studio.py release --candidate <tag>`).
