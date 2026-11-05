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

### 1. Start Omnigent on your Mac

The existing Egzakta checkout has a built supervisor and installed dependencies.
Run this in macOS Terminal:

```sh
cd /Users/pavluhin/Documents/Projects/Work/egzakta/omnigent
export PATH="$PWD/.omnigent/tools/bin:$HOME/.local/bin:$PATH"
.omnigent/build/cargo/debug/omnidev
```

The `PATH` line selects the checkout's compatible `uv` and makes your agent
CLIs visible to the host. `omnidev` starts the server, local execution host and
web UI. It opens the browser when ready. If needed, copy the exact `ui` URL
from its header. The port is selected at startup.

Keep the terminal open while using Omnigent. Press `q` or `Ctrl+C` to stop it.
Starting again from this checkout reuses its local conversations and review
records. The `--clean` option wipes that state. See the
[supervisor guide](../../dev/omnidev/README.md#isolation) for storage and logs.

If you already use the Omnigent desktop app, choose **Server → Change
Server…** and enter the `server` URL from the same header. The desktop app
uses the backend URL. Your browser uses the `ui` URL. In either client,
select the host that `omnidev` already connected. See the
[desktop connection guide](../../web/electron/README.md).

### 2. Connect your existing agent accounts

The local host runs each CLI under your macOS user. Your saved CLI sign-ins,
configuration and skills are available there. In a second Terminal tab, check
the status for the agents you want to use. Run the sign-in command only if
needed, then check status again.

| Agent | Check sign-in | Sign in with your existing account |
| --- | --- | --- |
| Claude Code | `claude auth status --text` | `claude auth login --claudeai` ([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)) |
| Codex | `codex login status` | `codex login` ([OpenAI authentication docs](https://developers.openai.com/codex/auth)) |
| Cursor | `cursor-agent status` | `cursor-agent login` ([Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication)) |

On this Mac, `agent` and `cursor-agent` are two names for the same Cursor CLI.
`cursor` opens the editor. Choose the native **Cursor** agent in Omnigent to use
the CLI account.

In Omnigent, open **Settings → Harnesses** and select your Mac. Check the
readiness shown on the three cards. If you just signed in, use **Check again**
where available or wait for the next host refresh. If you changed `PATH`, quit
and restart the supervisor from the terminal with the new path.

### 3. Start your first session

1. Choose **New session** and select your Mac in the host picker.
2. Set the working directory to the root you want the agent to use. For the
   Egzakta workspace, choose `/Users/pavluhin/Documents/Projects/Work/egzakta`.
   For a single-repo session, choose that checkout's directory.
3. Pick **Claude Code**, **Cursor** or **Codex** and send a short prompt such as
   `Read-only: tell me the current directory and repository name.` Repeat with
   a new session for each agent you want to check. Cursor may be under
   **Other… → Cursor**.

If a ready agent is hidden in the picker, check **Settings → Appearance → Show
in agent pickers**. If a native terminal fails to open, run `tmux -V` in the
launch terminal. Native sessions require tmux 3.3 or newer. The supervisor's
`2` key shows the host log.

All three agents can run regular sessions. The **Pull Requests → Review**
reviewer picker in A3 supports **Codex** and **Claude Code**, including from a
workspace session you started with Cursor. Continue with
[Run a review locally](#run-a-review-locally).

### Fresh checkout setup

Install the prerequisites in [CONTRIBUTING.md](../../CONTRIBUTING.md#development-setup),
including a `uv` that satisfies [uv.toml](../../uv.toml). From the fork checkout:

```sh
uv sync --frozen --extra all --group dev
pnpm install --frozen-lockfile --filter web
cargo run --locked --manifest-path dev/omnidev/Cargo.toml
```

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
2. In the target repository, use `gh auth status` to check GitHub sign-in and
   `gh pr view 'PR_URL'` with your PR URL to confirm the host can read it. If
   needed, sign in with `gh auth login` on that host.
3. In your session, open the **Workspace** panel on the right. Its default
   toolbar uses icons. Hover over the branch icon between **Changes** and
   **Agents** to find **Pull Requests**. If the icon is unpinned, open **+ →
   Pull Requests**. In a multi-repo workspace, choose a checkout under
   **Workspace repositories** and select **Find branch PR**, then link the
   result. For any full PR URL, use **Link a PR**, paste the URL and select
   **Link**. In the PR details, open **Review** and choose **Codex** or
   **Claude Code**. Select a **Reviewer profile** or choose **Custom skill**
   to pick a native skill. Select a **Model** from the review host, then
   **Reasoning effort** where available. **Default** uses the harness's
   own configuration. Click **Run review**.
4. Inspect the summary and findings. Open a finding's line in **Changes** to
   compare the claim with the diff. Refresh the page and reopen **Review** to
   check that the saved run and its linked reviewer session return.
5. If the report is incomplete, open the reviewer session and ask it to repair
   the structured report in the same conversation. Return to **Review** and
   refresh the report.

The reviewer runs in the captured workspace with the selected agent's normal
permissions. It receives the PR diff at the captured base and head revisions.
The first version accepts GitHub text diffs up to 48 KiB. A moved revision
requires a fresh capture. Reports contain agent findings. Check evidence is A4.
See the [review API and state model](review-records.md) for retry behavior.

### Share reviewer profiles with a workspace

Open **Manage profiles** in the Review tab, or open **Reviewer profiles** in
Project settings after saving the host and working directory. Create a profile
with Markdown instructions, preview it, and save it. Optional harness, model and
reasoning defaults prefill the Review form and can be overridden for each run.
Archive a profile to hide it from new reviews, or restore it from the editor.

The editor saves managed profiles to `.omnigent-review-profiles.json` at the
workspace root. Keep this file in the team-controlled workspace for Git review
and use it on each execution host. Saving checks the version you opened.

To link existing guidance, place `review-profiles.yaml` in the working directory.
Each entry points to a trusted Markdown file inside that workspace. Keep the
profile files in the team-controlled meta-repo so a nested PR checkout cannot
change the instructions used to review it. Linked profiles show their source in
the editor. Use **Duplicate** to create a managed copy.

```yaml
version: 1
profiles:
  backend:
    label: Backend QA
    description: Review APIs, data access, and backend tests.
    file: platform/agents/KVARK/kvark-backend-qa.md
```

Egzakta's workspace references the shared Backend, Frontend, AI Quality,
and CI/CD QA lenses and keeps private-review versions of KVARK's Security
and Orchestrator instructions in the meta-repo.
The review uses the Markdown body as guidance for a single private review of
the captured PR diff. Orchestrator applies its routing criteria within that
review. Native tool permissions and the requested model come from the selected
harness and launch settings. History records the requested model and profile.
If the profile changes after capture, start a fresh review to use the new version.

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

## Use a multi-repo workspace

In the project settings for your Egzakta sessions, select your Mac and set
**Working directory** to `/Users/pavluhin/Documents/Projects/Work/egzakta`.
Leave **Random worktree** off so new sessions open the root that contains your
nested clones.

From **New session in project**, choose **Open workspace** to start an empty
session using the host, working directory and agent shown in the composer.
The Workspace panel opens with Files, Pull Requests and Agents available.
Use **+ → Reviewer profiles** to manage profiles before linking a PR. With a
native harness and an empty draft, the session opens in Terminal. An existing
draft carries into Chat for you to send when ready.

To start with a review, open **Pull Requests**, find the nested checkout’s
branch PR or link a known PR URL, then use **Review → Run review**. The
reviewer appears as a separate private session in the same project.

For any workspace, open **Project settings → Repositories**, or open
**Manage repositories** from the PR panel. The managers run on the connected
macOS or Linux host. Choose a scan depth from 0 to 5 and click **Scan
repositories**. Depth 0 checks the root, depth 1 includes its direct children,
and depth 2 includes one additional level. You can also add a checkout by its
path relative to the root.

Scanned checkouts start as **Reference**. Promote the repos you want to use as
PR targets to **Active**. Use **Excluded** to hide an entry from the PR picker,
or **Remove** to forget its assignment. **Save repositories** writes the depth
and assignments to `.omnigent-repositories.json` in the workspace. The scan is
local and reports when it reaches a limit. Repository roles control the harness
picker. The agent's filesystem access follows its native permissions.

Omnigent also reads a compatible `repos.yaml` to show declared checkouts in
**Workspace → Pull Requests → Workspace repositories**. For example, choose
`KVARK` for platform work and see its declared dependency on `Services`. Switch
to `Services` when you need its own branch PR. GitHub lookup checks that the
checkout's `origin` matches the declared or saved host, organization and repository.
Egzakta checkouts use `github.com/Egzakta-AILab`. **Find branch PR** looks for an
open PR from the exact local branch name. Use **Link a PR** with the full URL
for fork branches, closed PRs or another known PR.

The session and its reviewer start at the workspace root, where the native
harness resolves skills and rules through its normal configuration. Keep using
`org` to provision those assets. Select a review skill from the actual host
catalog in **Review**. A review pins the linked PR's repository, base and head
revisions. The workspace **Changes** tab shows the outer checkout's local Git
changes. Repo-specific check execution needs its own pinned checkout binding.
