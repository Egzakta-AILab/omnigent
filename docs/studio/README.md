# Egzakta development studio

This fork starts with a complete PR workflow on a laptop or a server. Creator
Mode follows it. Existing Codex and Claude Code skills perform the work;
Omnigent owns sessions, hosts and the application interface.

The [implementation plan](https://chatgpt.com/space/page_0dc939b0cb9c8191bfa031c3dadb65f1)
defines the delivery order. [baseline.json](baseline.json) records the source
revision at the start of this work. This checkout includes setup verification,
private review records and native reviews in the PR panel. Development and
acceptance use a local instance.

## Local checkout

Install the prerequisites in [CONTRIBUTING.md](../../CONTRIBUTING.md#development-setup),
including a `uv` satisfying [uv.toml](../../uv.toml). Use the repository's pinned
Python and pnpm versions. Keep the lockfiles unchanged during setup.

From this checkout:

```sh
uv sync --frozen --extra all --group dev
uv run --no-sync pre-commit install
pnpm install --frozen-lockfile --filter web
cargo run --locked --manifest-path dev/omnidev/Cargo.toml
```

`omnidev` starts the existing server, host and frontend supervisor. Open the
exact UI URL it prints, or connect the installed desktop app to its server URL.
Do not assume the default ports are available. `--pod-dir /absolute/path` selects
an explicit state directory; see its [options](../../dev/omnidev/README.md#options).

The pod isolates Omnigent's configuration, database, artifacts and logs. It
inherits the execution user's native agent credentials and skills. It is not an
OS sandbox. Keep the supervisor running while using the pod. Stopping it or
sleeping the laptop can interrupt local execution.

The settings already expose the setup details needed here:

1. Open **Settings → Harnesses** and select the intended host.
2. Check Codex and Claude Code readiness and their selected account.
3. Inspect the existing Skills, MCPs and Plugins inventories. Use the existing
   MCP tool probe when checking integration connectivity.
4. Open a session on that host and verify its workspace before starting work.

Organization skills remain provisioned through the existing organization tools.
Each host needs its own authorized native configuration. Selecting the server
in the desktop app does not move the laptop's skills or credentials there.

## Server from this fork

Use the maintained Docker deployment with the studio overlay. The overlay
builds the current checkout under a revision-specific image name, uses a
separate Compose project and enables authentication. This avoids accidentally
running the upstream `latest` image.

Prepare a private environment file using
[deploy/docker/.env.example](../../deploy/docker/.env.example). Set the database
password, the accounts or OIDC configuration and the externally reachable base
URL according to the [deployment guide](../../deploy/docker/README.md). Keep
secrets outside version control. Public access needs the deployment's supported
HTTPS/reverse-proxy configuration.

From a clean checkout of the chosen commit, on the server:

```sh
export STUDIO_REVISION="$(git rev-parse HEAD)"
docker compose --project-name egzakta-omnigent-studio \
  --env-file /absolute/path/studio.env \
  -f deploy/docker/docker-compose.yaml \
  -f deploy/docker/docker-compose.studio.yaml config --quiet

docker compose --project-name egzakta-omnigent-studio \
  --env-file /absolute/path/studio.env \
  -f deploy/docker/docker-compose.yaml \
  -f deploy/docker/docker-compose.studio.yaml up --build -d
```

Record the deployed image ID alongside the source commit. The upstream
Dockerfile resolves Python packages during its build, so rebuilding a commit
does not promise identical image bytes. Reuse the recorded image for an exact
deployment rollback, and follow the database migration compatibility rules.

The server container coordinates sessions; agent execution requires a host.
On the server's chosen execution account, install this same checkout, configure
its native agents and repository access, then register that host:

```sh
uv run --no-sync omnigent login https://studio.example.com
uv run --no-sync omnigent host --server https://studio.example.com
```

Keep that host running through the server's existing service supervision.
Connect the browser or desktop app to the server URL and choose this host.
Closing the laptop should leave server execution running; verify that behavior
with a real session before treating the setup as qualified.

## Read only setup receipt

Run the acceptance command against an explicit instance, owned host and
workspace. It reads existing diagnostics and inventory endpoints. It does not
start sessions, refresh accounts, probe MCP tools or call models.

```sh
uv run --no-sync python scripts/verify_studio.py setup \
  --mode local --server http://127.0.0.1:6767 \
  --host-id YOUR_HOST_ID --workspace /absolute/path/to/KVARK \
  --harness codex-native --harness claude-native \
  --output .omnigent/studio/local-setup.json
```

Use the actual server URL printed by your supervisor. For a remote instance,
authenticate the CLI to that instance, use `--mode server`, and supply the
workspace path as it exists on its selected execution host. The verifier uses
the existing saved login for the supplied URL. Supply only a trusted instance;
URLs containing credentials, query strings or fragments are rejected.

Add repeated `--require-skill NAME` and `--require-mcp NAME` options to require
your selected review assets. The MCP endpoint lists user-level configuration;
project and session MCP bindings need separate verification. A configured MCP
name proves inventory presence; its connectivity and usable permissions need
separate checks. Startup evidence contains source labels, not environment values
or command arguments.

A successful preflight means its required setup signals were observed. It does
not prove a model turn, PR access, continuation, reconnect or the full review
workflow. Those qualifications remain explicitly `not_run`. Unknown readiness,
incomplete discovery and missing required assets keep preflight blocked.

## Acceptance before the PR workflow

For each agent, Codex and Claude Code, on each setup, laptop and server:

1. Retain a successful setup receipt for the exact host and workspace.
2. Start a harmless native agent turn, then continue in the same session.
3. Close and reopen the client; confirm the same session and history return.
4. For the server setup, disconnect the laptop while a turn is active and
   verify that execution remains on the server host.
5. Check GitHub PR access from that execution host. The server's GitHub App
   connection alone does not prove the host can read a repository.
6. Record failures and interruptions, including whether cancellation or restart
   left execution status unknown. Never relaunch an uncertain run automatically.

Use the repository's [isolated verification skill](../../feature-map/skills/verify-omnigent/SKILL.md)
for mock-provider qualification. Mock execution, actual vendor calls and human
inspection are separate evidence categories. A `/health` response is liveness,
not proof of agent or workflow readiness.

## Delivery order

| Work item | Deliverable |
| --- | --- |
| A1 | Setup and read-only preflight; native routes exercised locally with mock models |
| A2 | Private, revision-bound review records and authorized capture API |
| A3 | Run existing review skills from the PR panel and inspect findings |
| A4 | Execute checks and retain immutable proofs |
| A5 | Discuss, request fixes, review new revisions and publish selected feedback |
| A6 | Qualify the complete workflow on all four agent/setup routes |
| B1 | Discoverable contracts and one generated-panel contribution point |
| B2 | Generate and preview a useful QA panel |
| B3 | Enable, update and roll back its versioned UI package |

Full environment lifecycle management and the component map follow these two
releases. The initial execution-target binding is limited to what review
provenance needs.

## Run a review locally

1. Start the local instance, connect your host and open a session you own in
   a workspace where you already run Codex or Claude Code.
2. Attach a GitHub PR and open **Pull Requests → Review**. Choose the built-in
   reviewer and a review skill from the host's catalog, then select **Run review**.
3. Inspect the summary and findings. Open a finding's line in **Changes** to
   compare the claim with the diff. Refresh the page and reopen **Review** to
   check that the saved run and its linked reviewer session return.
4. If the report is incomplete, open the reviewer session and ask it to repair
   the structured report in the same conversation. Return to **Review** and
   refresh the report.

The reviewer runs in the captured workspace with the selected agent's normal
permissions. It receives the PR diff at the captured base and head revisions.
The first version accepts GitHub text diffs up to 48 KiB. A moved revision
requires a fresh capture. Reports contain agent findings. Check evidence is A4.
See the [review API and state model](review-records.md) for retry behavior.

Run the deterministic A2 record checks from this checkout:

```sh
uv run --no-sync python scripts/verify_studio.py contracts
```

The command retains its test log, JUnit report and JSON receipt under
`.omnigent/studio/evidence/`. It exercises local SQLite and controlled transport
responses. A3 coverage also includes the native launch and report contract tests
and a browser journey through the shared PR panel.

For a machine-readable check of a review you already ran, use its review ID
and the exact server and PR URLs:

```sh
uv run --no-sync python scripts/verify_studio.py review \
  --mode local --server http://127.0.0.1:6767 --review-id YOUR_REVIEW_ID \
  --harness codex-native --pr https://github.com/YOUR_ORG/YOUR_REPO/pull/17 \
  --output .omnigent/studio/review-receipt.json
```

Use `--harness claude-native` for Claude Code. The command checks the saved
revision, reviewer session and structured report through the instance API.
