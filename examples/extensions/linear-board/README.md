# Linear board pilot

From the Omnigent checkout, build and install the optional extension into the
Python environment that runs your Omnigent server.

```sh
pnpm --filter web exec vite build --config ../examples/extensions/linear-board/web/vite.config.ts
uv --no-config pip install --no-deps --python .venv/bin/python ./examples/extensions/linear-board
```

Restart the server from this checkout, then open **Linear** in the sidebar.
Enter a read-only Linear personal API key. The page displays the connected
account and workspace and groups a team’s issues by workflow status. Select a
ticket, choose a parent Omnigent project, and select **Start session**. If there is a linked session, select **Resume session**.
**Open task** shows the project’s saved issue details and session history.

The extension holds the key in memory and clears it when you disconnect or
leave the page. Use a trusted Omnigent instance because the server relays the
key to Linear. The project stores an issue reference and inherits session
defaults from the chosen parent. Starting the same issue again for the same
Omnigent account opens the existing project. The composer includes an issue
prompt and defaults to a fresh worktree. Confirm the correct repository,
particularly if the parent workspace contains multiple repositories.

This pilot uses owner-private projects and the existing composer worktree
flow. Project sharing, a persistent project checkout, and OAuth are later
work. Install or remove the Linear package independently of Review/QA.
