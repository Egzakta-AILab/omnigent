# Extensions

Omnigent extensions are operator-installed Python distributions that contribute
validated application metadata. V1 supports namespaced browser pages and links
in the primary sidebar.

## Trust model

Installing a Python distribution is a full-code-trust operation: Python entry
points execute in the Omnigent server process while their manifests are loaded.
Only administrators should install extension packages.

Browser UI receives a separate boundary. It runs in an opaque-origin sandboxed
iframe. The frame cannot read the parent React tree, cookies, authenticated
transport, or native bridges. Its content policy blocks fetches and external
subresources. The browser can still navigate the frame itself, so install only
reviewed bundles. Privileged actions use the permission-checked browser SDK.

```text
trusted Python package -> validated manifest -> server catalog
                                                |
                                      sandboxed browser page
                                                |
                                      checked MessageChannel API
```

## V1 contribution points

- `pages`: one-segment routes under `/extensions/{extension_id}/{route}`.
- `primary_navigation`: links rendered in the extension-owned slot between
  Inbox and Usage. `order` sorts only within that slot.

Command metadata, activation events, and `when` expressions are validated and
reserved but are not executed or evaluated in V1. Arbitrary React components,
DOM selectors, FastAPI routers, runner tools, and user-installed marketplace
packages are not supported.

## Creator Mode review panel pilot

Set `OMNIGENT_STUDIO_CREATOR_PILOT=1` on the server and restart. Open
**Creator Mode** from the Omnigent sidebar. For the Studio Compose overlay,
set the same variable in its environment before recreating the service.

1. Choose the review panel extension and select **Open authoring draft**.
   The native composer opens with your brief, the installed schema, a QA
   template, and any draft or diagnostic currently in the editor.
2. Choose an agent and an isolated workspace, then send the brief. Use **Back
   to Creator Mode** and paste the resulting JSON into **Panel definition**.
   The handoff keeps the brief and definition in memory until reload. **Load
   QA template** gives you a starting definition directly.
3. Paste a saved discussion link under **Saved review for preview**, then
   select **Use saved review** and **Build preview**. The preview groups
   check receipts from that revision. The selected review also supplies the
   project for installation.
4. Edit and rebuild after a diagnostic. **Download definition** saves the
   validated JSON. Once installed, **Open review** lets you try the panel's
   proof actions in its review discussion.

The preview compiles a bounded JSON definition and renders it with native
Omnigent components. The definition can choose grouping, filtering, labels,
and proof actions. It cannot add executable code, transport calls, or
publication actions.

Inspect the installed contract and template with
`python scripts/verify_studio.py creator-contracts`. To verify that a
running instance accepts a definition for a specific saved review:

```bash
python scripts/verify_studio.py creator-preview --mode local \
  --server http://127.0.0.1:6767 --review <run-id> --definition review-panel.json
```

Omit `--definition` to use the installed template. For a remote instance,
use `--mode server` and its HTTPS URL. The command checks the API binding and
digest. Use the review discussion to verify receipt navigation and prefill.

### Install, update, and roll back

Build a preview, inspect its behavior, and select **Enable preview** in
**Project panel installation**. Approve the displayed capabilities on the first
installation and whenever a version changes them. The active panel appears
in saved review discussions for your account in the same project.

To update, choose **Edit installed definition**, revise the JSON, and build
and install the update. The server retains up to 32 versions. **Version
history** shows the version and package digest. Choose an older version and
select **Roll back**, or use **Disable panel** to retain history while removing
the active panel. If a saved version becomes incompatible, select a compatible
version from history.

After an instance restart, verify the installed version against its downloaded
definition with:

```bash
python scripts/verify_studio.py release-b --mode local \
  --server http://127.0.0.1:6767 --review <run-id> \
  --package review-panel.json --version 1
```

For a disabled selection, add `--expect disabled`. For an HTTPS deployment use
`--mode server` and its URL. Each instance stores its own installations.

### Operator-installed browser panels

On the server, set `OMNIGENT_STUDIO_CREATOR_PILOT=1` and restart with a reviewed
extension installed. Add a `review_panel` field to its `ExtensionManifest`:

```python
review_panel=ReviewPanelContribution(
    slot="review.panel",
    contract_version="1",
    view="qa-checks",
    title="QA checks",
    capabilities=("review.read", "review.openProof", "review.attachProof"),
)
```

Import `ReviewPanelContribution` from `omnigent.extensions`. The panel title
appears in an accessible review discussion. Opening it loads the installed
bundle and binds its declared `context.review` methods to that saved review.
An extension can page through findings and check summaries. It can focus a
receipt in the native review UI or select it in **Ask an agent**. The person
using the form submits the question.

Pass `--manifest panel.json` to `creator-contracts` to validate a browser
contribution before installing its package.

## Installation and diagnostics

Declare an `omnigent.extensions` entry point, install the wheel in the server's
Python environment, and restart the server. Installation implies enablement for
V1; there is no separate enable switch.

```bash
omni extensions list
omni extensions doctor publisher.extension
```

A broken extension does not stop healthy extensions or the server. Public
catalog entries omit rejected extensions. Administrators can inspect concise
load and asset errors through `doctor` or `/v1/extensions/diagnostics`.

![Hello Page reference extension](../demo/extensions-hello-page.png)

See [Extension manifest](extension_manifest.md),
[Browser extensions](browser_extensions.md), and the
[Hello Page example](../../examples/extensions/hello-page/README.md).
