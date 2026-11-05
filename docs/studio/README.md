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

### Trigger a private review from GitHub

Run the webhook receiver from this fork alongside the Omnigent server. Give
its bot account a dedicated project and connected sandbox host with access to
the approved checkouts. Native reviewers use that host's normal permissions,
so use a sandbox with only the credentials the reviewers need.

Each route pins a repository ID, allowed base branches, a project, a host
directory, and a roster. Use the canonical absolute workspace path reported
by that host. Add more routes for other repositories in a meta workspace.
Keep drafts and fork PRs disabled unless you want that route to review them.

```json
{
  "routes": [
    {
      "id": "kvark",
      "github_repository": "Egzakta-AILab/KVARK",
      "github_repository_id": 123456789,
      "base_branches": ["main"],
      "secret_env": "OMNIGENT_STUDIO_HOOK_KVARK",
      "project_id": "11111111111111111111111111111111",
      "host_id": "22222222222222222222222222222222",
      "workspace": "/srv/egzakta-org",
      "repository": "KVARK",
      "collaborators": ["reviewer@example.com"],
      "publishers": ["reviewer@example.com"],
      "prepare_inline_drafts": true,
      "reviewers": [
        {
          "agent_id": "33333333333333333333333333333333",
          "harness": "codex-native",
          "profile_id": "backend",
          "profile_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }
      ]
    }
  ]
}
```

#### Run the receiver with Compose

Start the Studio server and sign in as the non-admin bot that owns the project
and host. Save the bot's CLI login in a private directory dedicated to the
receiver. Use the same HTTPS URL for login and `STUDIO_WEBHOOK_SERVER_URL`.

```sh
install -d -m 700 /absolute/path/studio-webhook-auth /absolute/path/studio-webhook-inbox
OMNIGENT_DATA_DIR=/absolute/path/studio-webhook-auth \
  omnigent login https://studio.example.com
```

Save the route JSON outside the checkout. In another private file, put one
unquoted `NAME=value` line per webhook secret. Use the exact name from each
route's `secret_env` and at least 32 bytes per value. Add these paths to your
Studio Compose environment file.

```dotenv
STUDIO_WEBHOOK_SERVER_URL=https://studio.example.com
STUDIO_WEBHOOK_ROUTES_FILE=/absolute/path/studio-webhooks.json
STUDIO_WEBHOOK_SECRETS_FILE=/absolute/path/studio-webhooks.env
STUDIO_WEBHOOK_AUTH_DIR=/absolute/path/studio-webhook-auth
STUDIO_WEBHOOK_STATE_DIR=/absolute/path/studio-webhook-inbox
STUDIO_WEBHOOK_UID=1000
STUDIO_WEBHOOK_GID=1000
```

Use the numeric UID and GID of the account that owns these files and
directories. Get them with `id -u` and `id -g` on the Docker host.

For the bundled Caddy proxy, use these overlays from the same checkout and
Compose project as the running server. The public URL must resolve from the
receiver container. The receiver uses the same fork image as the server and
keeps its SQLite inbox in the separate state directory.

```bash
export STUDIO_REVISION="$(git rev-parse HEAD)"
studio=(
  --project-name egzakta-omnigent-studio
  --env-file /absolute/path/studio.env
  -f deploy/docker/docker-compose.yaml
  -f deploy/docker/docker-compose.studio.yaml
  -f deploy/docker/docker-compose.https.yaml
  -f deploy/docker/docker-compose.studio-webhooks.yaml
  -f deploy/docker/docker-compose.studio-webhooks.https.yaml
)
docker compose "${studio[@]}" config --quiet
docker compose "${studio[@]}" build omnigent
docker compose "${studio[@]}" up -d --no-build
docker compose "${studio[@]}" exec studio-webhooks python -m omnigent.studio.pr_webhook \
  --database /var/lib/studio/webhooks.sqlite --status
```

With an existing reverse proxy, omit both HTTPS overlays and route the
webhook path to `studio-webhooks:8769` on the Compose network. Apply changes
to routes or HMAC secrets with `up -d --force-recreate studio-webhooks`
using the same overlay set. Keep the inbox directory through upgrades and
rollbacks so delivered revisions remain deduplicated. The receiver refreshes
the bot's access token from its stored CLI login. Run the login command again
if the refresh grant expires or is revoked.

#### Run the receiver as a standalone process

Provide the HMAC secrets through the service's secret store. Supply a
first-party bearer in `OMNIGENT_STUDIO_REVIEW_TOKEN`, or use
`--use-stored-auth` with the dedicated CLI login directory. Mount the inbox
path on persistent storage.

```sh
uv run --no-sync python -m omnigent.studio.pr_webhook \
  --server https://omnigent.example.com \
  --config /etc/omnigent/studio-webhooks.json \
  --database /var/lib/omnigent/studio-webhooks.sqlite
```

Route GitHub's JSON **Pull requests** webhook over HTTPS to
`/webhooks/github/kvark`. Use the route's secret in GitHub. The receiver
checks the HMAC-SHA256 signature on the original request body and acknowledges
after saving the eligible delivery. [GitHub webhook validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

Opened, reopened, ready-for-review, and synchronized PRs create one Studio run
per base and head pair. A coordinator, if configured, starts a planning session
and waits for roster approval in Studio. The bot finds the run in its project's
PR history.

Optional `collaborators` lists existing Omnigent account IDs for that route.
Each run saves its access grants with its captured revision. Coworkers open
**Inbox → All → Shared PR reviews** to read the review, inspect attached
evidence, discuss a finding, or ask an agent using their own connected host.
Sharing must be **On** to grant access. Leaving the list empty keeps new runs
private. Removing an ID from the route affects future runs. Revoke access to
earlier runs from **Share this review** as the review owner.

Optional `publishers` selects accounts from the collaborator list. The owner
can also choose **Share this review → Allow publishing** for an existing
collaborator. In **Publish inline drafts**, a publisher can preview the
review owner's drafts and their own drafts, then confirm selected comments.
Each comment is sent separately to its code range through the review workspace's
configured provider account. Omnigent checks the revision and records the
account that confirmed the delivery. The owner can remove publishing access
while keeping the collaborator's discussion access.

Enable `prepare_inline_drafts` to have the receiver wait for all selected
reviewers. With a coordinator, it also waits for roster approval. It checks
that the PR revision still matches and saves up to eight critical or high
findings with checked line citations. The owner can edit the drafts. A
publisher finds them in **Publish inline drafts** and sends selected drafts
with **Confirm publication**.
The status command shows `drafting` while reports are pending, `drafted`
when preparation finishes, and `draft_skipped` for incomplete reports or
revision conflicts. Exhausted polling or a permanent API error shows
`draft_failed`. A run with no qualifying findings produces zero drafts.

```sh
uv run --no-sync python -m omnigent.studio.pr_webhook \
  --database /var/lib/omnigent/studio-webhooks.sqlite --status
```

A capture conflict is skipped because Studio could not confirm the delivered
revision. If start delivery is uncertain, the receiver reads the saved run
and records the result without issuing another start. Inspect the saved run
before deciding how to proceed. Use `--mode local` with an isolated loopback
Studio instance for a test fixture.

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

## Release A matrix

Use `python scripts/verify_studio.py release-a --schema` to inspect the JSON
evidence format. Enter the candidate checkout commit and the instance, host,
workspace, review, finding, question, and candidate IDs for each of the four
routes. For a source launch, set `OMNIGENT_BUILD_SOURCE_COMMIT` to the verified
checkout commit before starting Omnigent. The Studio Compose image records
`STUDIO_REVISION` as build metadata.

```sh
python scripts/verify_studio.py release-a \
  --pr https://github.com/Egzakta-AILab/KVARK/pull/1488 \
  --second-repo /absolute/path/to/second-checkout \
  --manifest .omnigent/studio/release-evidence.json \
  --output .omnigent/studio/release-a.json
```

The verifier reads the selected instance, checks the saved native review and
proof lifecycle, and verifies the second repository and any confirmed inline
publication. Server lanes require non-loopback HTTPS. Record each native
workflow on the candidate build, plus disconnect, restart, desktop/browser
parity, and a provider head change. Put the operator-reviewed recordings or
logs under the manifest directory and include their run ID, checkout commit,
reviewer name, and SHA-256. The head-change record also names the PR and its
previous and new head SHAs. The report distinguishes retained observations
from API reads. Run without a manifest to list missing evidence without
contacting an instance.

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
requires a fresh capture. Review reports and reproduction receipts are separate.
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

Open the project or choose **New session in project**. Workspace opens beside
the composer using its selected host and directory. Browse **Files**, choose
an agent, or manage reviewer profiles under **Profiles**. Use **Add file to
prompt** or **Add PR to prompt** to carry a reference into your first message.

To start with a review, select a repository under **Pull Requests**, then
choose an open PR or paste its GitHub URL. The **Diff** tab opens first. Inspect
a changed file, then choose **Review → Run review**. The private reviewer
session opens on a report with the captured revision, reviewer settings,
summary, and findings. **Open transcript** shows the original exchange. In
the PR’s **Review** tab, use **Show in diff** to jump to the cited file and
line for a saved finding on the current revision.

To group reviewers on the same revision, configure one reviewer and select
**Add reviewer to run**. Repeat for the other profiles or skills, then select
**Run selected reviewers**. A run captures the roster before launching its
private reviewer sessions. Under **Review runs**, open the consolidation or
select an individual reviewer to inspect the report. The run keeps its
first complete report for each reviewer once read and shows a reason when
a reviewer cannot start. A run with missing reports shows partial coverage.

To let an agent choose the roster, configure a reviewer and select **Use as
coordinator**. Add reviewer candidates and check **Required** beside anyone the
run must include. Select **Ask coordinator to plan**. The run shows which
candidates the coordinator chose or skipped and why. **Approve plan and start
reviewers** saves those decisions and starts the selected reviewers. The planning
session is available from the run, and consolidation counts the selected
reviewers. Each review launch checks that the PR still matches the captured diff.

New runs save the revision-checked diff before dispatch. **Show in captured
diff** opens a finding's cited line from that saved copy, even after the PR
advances. Findings whose path, side, or line is absent from the changed rows
appear under **Unverified citations** in the individual report. Earlier runs
without a saved diff show that citation checks are unavailable.

To verify a saved run independently, supply the PR URL and the full revision
SHAs you expect it to have reviewed.

```sh
uv run --no-sync python scripts/verify_studio.py review-run \
  --mode local --server http://127.0.0.1:6767 --run-id RUN_ID \
  --pr https://github.com/YOUR_ORG/YOUR_REPO/pull/17 \
  --base BASE_SHA --head HEAD_SHA --minimum-reviewers 2 --json
```

The receipt checks the selected roster, report coverage, the patch digest,
changed-line citations, and attribution of consolidated findings. For
coordinated runs it also checks the approved decisions, required reviewers, and
whether a skipped candidate has a session.

### Reproduce a finding on both revisions

On the Mac or Linux machine that has Docker and a checkout containing both
commits, choose a locally cached test image with `/bin/sh`, `tar`, `timeout`,
and your test dependencies. Run the command you want to compare against the
saved review run:

```sh
uv run --no-sync python scripts/verify_studio.py reproduce \
  --mode local --server http://127.0.0.1:6767 --run-id RUN_ID \
  --pr https://github.com/YOUR_ORG/YOUR_REPO/pull/17 \
  --repository /path/to/repo --image python:3.12-alpine \
  --output .omnigent/studio/reproduction.json -- python path/to/check.py
```

Use `--mode server` and the HTTPS instance URL for a hosted review run.
The command runs on the machine where you invoke it. If its Docker socket is
outside `/var/run/docker.sock`, pass the absolute socket path with `--socket`.
For a Git-only check, use `--base` and `--head` with full commit IDs in place of
the saved-run options.

Each container gets a Git archive of one commit. Committed export attributes
apply. Local checkout attributes, dirty files, Git history, and submodule
contents are outside the input. Use an image with dependencies already
installed because containers run with networking disabled. The
private receipt records the selected image ID, each archive digest, elapsed
time, exit code, and up to 8 KiB of combined output per side. Elapsed time
includes container startup and extraction. Print measurements from your check
to include them in the output. The CLI exits successfully when both commands
ran to completion, even when a test reports a failure. Shell exit codes 126
and 127 are reserved for invocation failures.

Open **Reproductions** on a finding in **Review runs**, choose the JSON file,
and select **Attach receipt**. The panel accepts receipts for that run's
captured revision and diff. It marks the file as an uploaded receipt and shows
the base and head results separately. **Copy rerun command** uses the same
image ID and arguments. Run it on a Docker-enabled machine with the required
checkout. Attached receipts follow the review run's private access rules.

### Check a proposed fix

Open **Discuss finding → Propose a fix** and choose an agent. Preview its
private proposal and select **Download patch**. You can also supply your own
Git text patch against the review's captured head. Run the same check on that
head and on a temporary snapshot with the patch applied.

```sh
uv run --no-sync python scripts/verify_studio.py reproduce \
  --mode local --server http://127.0.0.1:6767 --run-id RUN_ID \
  --pr https://github.com/your_org/your_repo/pull/17 \
  --repository /path/to/repo --image "$TEST_IMAGE_ID" \
  --fix-patch /path/to/proposed.patch \
  --output .omnigent/studio/fix-check.json -- python path/to/check.py
```

The patch is applied in a temporary Git index. Both snapshots run in separate
containers with networking disabled. Use a locally installed image ID for
`TEST_IMAGE_ID`. If the full export exceeds 128 MiB, repeat `--include-path`
for the tracked directories or files required by the check. Each selected path
must exist in both snapshots. The receipt records the path scope, saved review
and head, patch digest, temporary commit, image, and both check results.
Keep the patch file to reproduce the comparison. The
command exits zero when the check fails on the captured head and passes
with the patch. The receipt describes this check on these two snapshots.

From the finding, open **Reproductions**, choose the fix-check JSON receipt
and the matching patch, and select **Attach receipt**. The attached check
shows both results, the export scope, and the proposed diff. People with
access to the saved review can inspect the attachment.

When the candidate passes the check, the review owner can select
**Re-review candidate**. Omnigent checks that the original PR still matches
the capture and starts a separate private run with the original selected
reviewers. This run reviews the proposed delta from the captured PR head to
the candidate snapshot. Open it from the fix attachment or the review run
list. Its discussion links back to the original finding.

To verify a saved lifecycle, provide the original run, a proof-backed question
about its failing head check, and the candidate that passed its fix comparison.

```sh
uv run --no-sync python scripts/verify_studio.py lifecycle \
  --mode local --server http://127.0.0.1:6767 \
  --pr https://github.com/your_org/your_repo/pull/17 \
  --run-id RUN_ID --finding FINDING_KEY --question QUESTION_ID \
  --candidate CANDIDATE_RUN_ID --output .omnigent/studio/lifecycle.json
```

The command reads saved records and verifies the receipt digests, revision
bindings, selected reviewer reports and diff citations for both runs. Use
`--mode server` for a trusted remote instance. Client reconnect, deployed
execution and GitHub publication have separate acceptance checks.

### Draft a sandbox check with an agent

Open a finding and choose **Draft sandbox check**. Pick a reviewer or native
agent and describe what the check should prove. The private proposal shows the
command, Git paths, timeout, and evidence filenames. Review those details
before choosing a Docker image ID installed on the workspace host. Select
**Run on workspace host** to approve the proposal. Omnigent saves the dispatch
and shows progress in the finding. Reopening the review shows the same check.
The host tests both saved revisions and Omnigent attaches the receipt and the
files it can verify. An unconfirmed dispatch requires a fresh proposal for
another host run.

To run it from a terminal, expand **Run from terminal**, select a checkout
with both commits, and choose **Copy sandbox command**. Run it from an Omnigent
checkout. If an expected file is missing, the receipt is available locally
for inspection and can be attached with `attach-proof`.

The result shows the agent proposal ID alongside the reproduction receipt.
Coworkers with review access can inspect the attached results.

### Attach media and measurements to a finding

Open **Reproductions** under a finding. In **Screenshots, recordings, and
measurements**, select a PNG or JPEG screenshot, an MP4 or WebM recording,
or a measurements JSON file. Images can be up to 4 MiB and recordings up
to 16 MiB. Choose **Attach evidence**. People with access to the saved review
can preview or download the evidence. Each attachment shows the saved PR
head and the file's SHA-256.

An agent can produce the following JSON during a check. Include enough
detail in the method for a coworker to repeat the measurement. Omit
`baseline` if you measured only one revision.

```json
{
  "method": "Median of five runs with the same 100-document fixture.",
  "metrics": [
    {"label": "Indexing latency", "unit": "ms", "baseline": 420, "observed": 610}
  ]
}
```

These files are labeled **Uploaded**. Attach the check receipt separately
when you also want to show which command ran and what it returned.

For a check that writes its own files, add `--capture-evidence FILENAME` to
the `reproduce` command before `--`. The check writes the named files under
`/evidence` inside each container. Choose up to two filenames, such as
`measurements.json` and `replay.webm`. The verifier saves the resulting files
beside the receipt in `<receipt>.files/base` and `<receipt>.files/head` and
records their hashes. The command reports any missing file. For a large
repository, repeat `--include-path` for the paths the check needs. The receipt
records that export scope.

To attach the saved check and its files together, choose **Copy finding key**
in the finding's **Reproductions** panel and run:

```sh
uv run --no-sync python scripts/verify_studio.py attach-proof \
  --mode local --server http://127.0.0.1:6767 \
  --finding FINDING_KEY --receipt .omnigent/studio/reproduction.json
```

Use the same receipt if an upload is interrupted. Omnigent checks the files
against the receipt and shows the associated check, commit, and base or head
side. This uses the same captured run when the PR advances.

### Discuss a saved review

Select **Discuss finding** to open its thread, or **Discuss review** to open
the full captured diff. Click a line number or drag across one side of a hunk
to select a range. Replies stay in Omnigent and remain attached to that saved
revision.

Select **Draft inline comment** to save feedback for the selected finding or
line range. The draft records the path, side, and range on the captured PR.
You can edit or discard your draft from the same thread. Each author sees
their own drafts, including in a review shared with teammates.

The review owner can select drafts in **Publish inline drafts**, choose
**Preview selected**, and confirm with **Publish inline comments**. The preview
shows the exact text, lines, and PR revision. Omnigent checks the revision again
at confirmation and posts each selected draft as its own thread from the
captured workspace's configured account. A posted draft links to its provider
thread. If delivery is unconfirmed, find the inline comment on GitHub and paste
its **Copy link** URL into **Check delivery**. Studio reads that comment and
checks its delivery reference, text, and original diff anchor before updating
the draft. Comments sent before Studio added delivery references can be checked
manually on GitHub. The current Studio capture flow supports GitHub.

Before confirming, **Download preview** saves the exact proposed comments.
After publication, the review owner can verify one comment using the saved
preview and a GitHub CLI login with read access to the repository.

```sh
uv run --no-sync python scripts/verify_studio.py publication \
  --mode local --server http://127.0.0.1:6767 \
  --preview /path/to/publication-preview.json --draft DRAFT_ID \
  --author GITHUB_LOGIN --output .omnigent/studio/publication.json
```

The command compares the saved confirmation with GitHub's original commit,
diff location, body, and author. Find the draft ID in the downloaded JSON.

On a multi-user instance with sharing enabled, the run owner can open **Share
this review**, grant access to an existing Omnigent account ID, and send the
link using **Copy review link**. A collaborator can see this review's captured
diff, finding summaries, and uploaded reproduction output. They can reply
while the instance allows editing. The grant covers this run within its project
and repository. **Revoke access** removes the collaborator's review and
receipt access. Capture a new run to share a review from before the instance
recorded the owner's account identity with each capture.

### Ask an agent from a review thread

Select **Ask an agent** on a finding or diff range. Choose a native Codex or
Claude Code agent, then select a model from the execution host and set its
reasoning effort where available. **Agent default** uses the native agent's
configuration. **Saved reviewer settings** reuses the captured agent, model,
and effort in a fresh session. The owner can use the saved workspace.
Collaborators choose a connected host and working directory under their own
account.

For a finding with a saved reproduction or fix comparison, use **Include
check receipt** before sending. The agent gets the selected revisions, check
outcomes, and bounded output excerpts. Private and shared answers show the
receipt identifier beside the question.

**Ask privately** sends the question and captured diff to the agent. The
session uses the selected native agent's permissions. It receives numbered
rows near the selected line. Omnigent checks each cited path, side, line and
quote against the saved diff. A failed check leaves the reply available in
the private session for inspection. When the answer is ready, **Share answer**
adds it and its matched excerpts to the internal thread. The excerpts show
which text the agent cited. Judge its interpretation separately.

For any workspace, open **Project settings → Repositories**, or open
**Manage repositories** from the PR panel. The managers run on the connected
macOS or Linux host. Choose a scan depth from 0 to 5 and click **Scan
repositories**. Depth 0 checks the root, depth 1 includes its direct children,
and depth 2 includes one additional level. You can also add a checkout by its
path relative to the root.

Scanned checkouts start as **Reference**. Promote the repos you want to use as
review targets to **Active**. Use **Excluded** to hide an entry from the PR picker,
or **Remove** to forget its assignment. **Save repositories** writes the depth
and assignments to `.omnigent-repositories.json` in the workspace. The scan is
local and reports when it reaches a limit. Repository roles control the harness
picker. The agent's filesystem access follows its native permissions.

Omnigent also reads a compatible `repos.yaml` for declared checkouts. Select
`KVARK` for platform PRs or `Services` for its own PRs. GitHub lookup checks
the checkout's `origin` against the declared or saved repository. Egzakta
checkouts use `github.com/Egzakta-AILab`. In an existing session, **Find branch
PR** looks up the checkout's current branch. On the project screen, **Open PR**
accepts the full GitHub URL for a known PR in the selected repository.

The session and its reviewer start at the workspace root, where the native
harness resolves skills and rules through its normal configuration. Keep using
`org` to provision those assets. Select a review skill from the actual host
catalog in **Review**. A review pins the linked PR's repository, base and head
revisions. The workspace **Changes** tab shows the outer checkout's local Git
changes. Repo-specific check execution needs its own pinned checkout binding.
