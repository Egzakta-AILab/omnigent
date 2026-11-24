# Open workspace files in an editor

The Files panel and file viewer have an outlined **Open** button. Click it to
open the current folder or file in the preferred editor, or use its chevron to
choose VS Code or Cursor. The selection is shared between both controls.

Remote editor links work in the browser and desktop app. Install the selected
editor and configure its Remote SSH support on the machine where you click the
button. The browser or desktop shell may ask permission to open the editor's
protocol link. Local file links are offered only when the desktop app can
identify the session's host as its own machine. Mobile shells hide the controls.
The browser uses a normal link to invoke the editor, so the page's `frame-src`
policy does not need to allow editor protocol schemes.

Remote controls require an online host with a supported SSH alias. Configure
that alias in your local SSH configuration. For sandbox providers the UI
derives it from the provider and canonical host name. For user-connected hosts,
the UI reads the machine-type label through the authenticated session file API.
It ignores display names (including IP-based names) when determining the type.
Unsupported or ambiguous labels and invalid canonical names disable the link.
Discovery is cached per session and host for five minutes and shared by the
folder and file controls. Clicking Open performs no metadata request.
Remote links currently support POSIX paths, including Arca and Arclet workspaces.

Other remote providers, custom aliases, missing or offline host rows, invalid
Arclet names, and unreadable or malformed metadata hide the remote controls.
The existing file API's session permissions and filesystem reach still apply;
the UI does not bypass them. Cached discovery cannot keep an offline host's
controls visible. Host-list updates refresh availability and provider aliases.
No new host protocol, server API, or database migration is required. The browser
cannot check whether an editor or SSH alias is installed locally.

To verify, open an Arca or Arclet session, choose **Files**, and click **Open**
beside the folder path. Accept the protocol prompt and confirm the editor's
remote indicator names the intended host. Open a file in the viewer and repeat;
the editor should select that file on the same remote machine. Repeat in the
desktop app and a browser, and check the menu remembers the chosen editor.
