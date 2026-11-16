import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ReadableMarkdown } from "../../../../../web/src/components/ReadableMarkdown";
import {
  defineExtension,
  type Disposable,
  type ExtensionContext,
} from "@omnigent/extension-sdk";
import "./style.css";

type Named = { id: string; name: string };
type Directory = {
  viewer: Named;
  organization: Named;
  teams: { nodes: Named[] };
};
type Issue = {
  id: string;
  identifier: string;
  title: string;
  state: Named;
  project: Named | null;
  assignee: Named | null;
};
type Board = {
  team: {
    issues: {
      nodes: Issue[];
      pageInfo: { hasNextPage: boolean; endCursor: string | null };
    };
  };
};
type IssueDetails = { issue: { description: string | null; url: string } };

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function selectField(label: string, rows: Named[], value: string) {
  const field = el("label", label);
  const input = el("select");
  for (const row of rows) {
    const option = el("option", row.name);
    option.value = row.id;
    input.append(option);
  }
  input.value = value;
  field.append(input);
  return { field, input };
}

function mount(context: ExtensionContext, root: HTMLElement) {
  let token = "";
  let directory: Directory | undefined;
  let team = "";
  let scope = "mine";
  let search = "";
  let board: Board["team"] | undefined;
  let issues: Issue[] = [];
  let selected: Issue | undefined;
  let existingTask: Awaited<
    ReturnType<ExtensionContext["linear"]["findTask"]>
  > = null;
  let detail: IssueDetails["issue"] | undefined;
  let parents: Named[] = [];
  let parent = "";
  let busy = false;
  let error = "";
  let generation = 0;

  async function load(action: () => Promise<void>) {
    const current = ++generation;
    busy = true;
    error = "";
    render();
    try {
      await action();
    } catch (reason) {
      if (current === generation)
        error = reason instanceof Error ? reason.message : "Request failed";
    } finally {
      if (current === generation) {
        busy = false;
        render();
      }
    }
  }

  async function readBoard(after?: string) {
    const result = await context.linear.read<Board>({
      token,
      team,
      after,
      search,
      assignee: scope === "mine" ? directory?.viewer.id : undefined,
    });
    board = result.team;
    issues = [
      ...new Map(
        [...(after ? issues : []), ...board.issues.nodes].map((issue) => [
          issue.id,
          issue,
        ]),
      ).values(),
    ];
  }

  function button(text: string, click: () => void, disabled = busy) {
    const result = el("button", text);
    result.type = "button";
    result.disabled = disabled;
    result.onclick = click;
    return result;
  }

  async function saveTaskSnapshot(issue: Issue, templateProjectId: string) {
    const snapshot = detail;
    const workspaceId = directory?.organization.id;
    if (!snapshot || !workspaceId)
      throw new Error("Reconnect Linear to load the task.");
    return context.projects.create({
      name: `${issue.identifier} · ${issue.title}`.slice(0, 100).trim(),
      templateProjectId,
      sourceKey: `linear:${workspaceId}:${issue.id}`.toLowerCase(),
      linearIssue: {
        identifier: issue.identifier,
        title: issue.title,
        description: (snapshot.description ?? "").slice(0, 16384),
        url: snapshot.url,
      },
    });
  }

  let markdownRoot: Root | undefined;

  function render() {
    markdownRoot?.unmount();
    markdownRoot = undefined;
    const main = el("main");
    const header = el("header");
    header.append(el("h1", "Linear"));
    if (directory)
      header.append(
        el("p", `${directory.organization.name} · ${directory.viewer.name}`),
      );
    main.append(header);
    if (!directory) {
      const form = el("section");
      form.className = "connection";
      const label = el("label", "Personal API key");
      const key = el("input");
      key.type = "password";
      key.required = true;
      key.maxLength = 256;
      key.autocomplete = "off";
      key.disabled = busy;
      label.append(key);
      const connect = el("button", busy ? "Connecting…" : "Connect Linear");
      connect.type = "button";
      connect.disabled = busy;
      form.append(
        label,
        connect,
        el(
          "p",
          "Use a read-only key. Omnigent keeps it in memory until you leave this page.",
        ),
      );
      connect.onclick = () => {
        if (busy || !key.value.trim()) return;
        token = key.value.trim();
        key.value = "";
        void load(async () => {
          const result = await context.linear.read<Directory>({ token });
          directory = result;
          team =
            (
              result.teams.nodes.find(
                ({ name }) => name === "KVARK DEVELOPMENT",
              ) ?? result.teams.nodes[0]
            )?.id ?? "";
          if (team) await readBoard();
        });
      };
      key.onkeydown = (event) => {
        if (event.key === "Enter") connect.click();
      };
      main.append(form);
    } else {
      const filters = el("section");
      filters.className = "filters";
      const teamSelect = selectField("Team", directory.teams.nodes, team);
      teamSelect.input.disabled = busy;
      teamSelect.input.onchange = () => {
        team = teamSelect.input.value;
        board = undefined;
        issues = [];
        selected = undefined;
        void load(() => readBoard());
      };
      const refreshIssues = () => {
        board = selected = undefined;
        issues = [];
        void load(() => readBoard());
      };
      const scopeField = selectField(
        "Assignee",
        [
          { id: "mine", name: "Mine" },
          { id: "everyone", name: "Everyone" },
        ],
        scope,
      );
      scopeField.input.disabled = busy;
      scopeField.input.onchange = () => {
        scope = scopeField.input.value;
        refreshIssues();
      };
      const searchForm = el("form");
      searchForm.className = "issue-search";
      const searchField = el("label", "Find an issue");
      const searchInput = el("input");
      searchInput.type = "search";
      searchInput.placeholder = "Title or issue ID";
      searchInput.value = search;
      searchInput.maxLength = 160;
      searchInput.disabled = busy;
      searchField.append(searchInput);
      const submitSearch = button("Search", () => {}, busy);
      submitSearch.type = "submit";
      searchForm.append(searchField, submitSearch);
      searchForm.onsubmit = (event) => {
        event.preventDefault();
        search = searchInput.value.trim();
        refreshIssues();
      };
      filters.append(
        teamSelect.field,
        scopeField.field,
        searchForm,
        button("Disconnect", () => {
          token = "";
          directory = board = selected = detail = undefined;
          issues = [];
          parents = [];
          search = parent = error = "";
          scope = "mine";
          render();
        }),
      );
      main.append(filters);
      if (selected) {
        const panel = el("section");
        panel.className = "detail";
        panel.append(el("p", selected.identifier), el("h2", selected.title));
        panel.append(
          el(
            "p",
            [
              selected.state.name,
              selected.project?.name,
              selected.assignee?.name,
            ]
              .filter(Boolean)
              .join(" · "),
          ),
        );
        if (detail?.description) {
          const description = el("div");
          description.style.maxWidth = "100%";
          panel.append(description);
          markdownRoot = createRoot(description);
          markdownRoot.render(
            createElement(ReadableMarkdown, { children: detail.description }),
          );
        } else if (detail) {
          panel.append(el("p", "No description on this issue."));
        }
        if (existingTask) {
          const task = existingTask;
          const issue = selected;
          panel.append(
            button(
              task.sessionId ? "Resume session" : "Start session",
              () => {
                void load(async () => {
                  if (task.snapshotTemplateId)
                    await saveTaskSnapshot(issue, task.snapshotTemplateId);
                  if (task.sessionId)
                    await context.navigation.openSession(task.sessionId);
                  else
                    await context.navigation.openNewSession({
                      projectId: task.projectId,
                      issue: issue.identifier,
                    });
                });
              },
              busy,
            ),
          );
          panel.append(
            button(
              "Open task",
              () => {
                void load(async () => {
                  if (task.snapshotTemplateId)
                    await saveTaskSnapshot(issue, task.snapshotTemplateId);
                  await context.navigation.openNewSession({
                    projectId: task.projectId,
                    issue: issue.identifier,
                  });
                });
              },
              busy,
            ),
          );
        } else if (parents.length) {
          const template = selectField(
            "Parent project",
            [{ id: "", name: "Choose a project" }, ...parents],
            parent,
          );
          const start = button(
            "Start session",
            () => {
              const issue = selected;
              if (!issue || !parent || !detail) return;
              void load(async () => {
                const project = await saveTaskSnapshot(issue, parent);
                await context.navigation.openNewSession({
                  projectId: project.id,
                  issue: issue.identifier,
                });
              });
            },
            busy || !parent || !detail,
          );
          template.input.disabled = busy;
          template.input.onchange = () => {
            parent = template.input.value;
            start.disabled = !parent || !detail;
          };
          panel.append(
            template.field,
            el(
              "p",
              "The project inherits session defaults. Confirm the repo and worktree in the composer.",
            ),
            start,
          );
        } else if (!busy)
          panel.append(
            el("p", "Create an Omnigent project to use as a template."),
          );
        panel.append(
          button("Back to board", () => {
            selected = detail = undefined;
            render();
          }),
        );
        main.append(panel);
      } else if (board) {
        const list = el("ul");
        list.className = "issue-list";
        if (!issues.length) main.append(el("p", "No matching issues."));
        for (const issue of issues) {
          const card = button("", () => {
            selected = issue;
            detail = undefined;
            existingTask = null;
            parent = "";
            void load(async () => {
              const [result, templates, task] = await Promise.all([
                context.linear.read<IssueDetails>({ token, issue: issue.id }),
                context.projects.list(),
                directory
                  ? context.linear.findTask({
                      sourceKey:
                        `linear:${directory.organization.id}:${issue.id}`.toLowerCase(),
                      issue: issue.identifier,
                    })
                  : null,
              ]);
              if (selected !== issue) return;
              existingTask = task;
              detail = result.issue;
              parents = templates;
            });
          });
          card.append(
            el("small", issue.identifier),
            el("strong", issue.title),
            el(
              "small",
              `${issue.assignee?.name ?? "Unassigned"} · ${issue.state.name}`,
            ),
          );
          const row = el("li");
          row.append(card);
          list.append(row);
        }
        main.append(list);
        const { hasNextPage, endCursor } = board.issues.pageInfo;
        if (hasNextPage && endCursor)
          main.append(
            button(
              "Load more issues",
              () => void load(() => readBoard(endCursor)),
            ),
          );
      } else if (!busy && !team)
        main.append(el("p", "This Linear account has no available teams."));
      else if (!busy)
        main.append(button("Retry team", () => void load(() => readBoard())));
    }
    if (busy) {
      const status = el("p", "Loading…");
      status.setAttribute("role", "status");
      main.append(status);
    }
    if (error) {
      const alert = el("p", error);
      alert.setAttribute("role", "alert");
      main.append(alert);
    }
    root.replaceChildren(main);
  }

  render();
  return () => {
    generation++;
    token = "";
    markdownRoot?.unmount();
    root.replaceChildren();
  };
}

let theme: Disposable | undefined;
let dispose: (() => void) | undefined;
defineExtension({
  async activate(context) {
    const root = document.getElementById("root");
    if (!root) return;
    dispose = mount(context, root);
    const current = await context.theme.getCurrent();
    document.documentElement.dataset.theme = current.theme;
    theme = await context.theme.subscribe(({ theme }) => {
      document.documentElement.dataset.theme = theme;
    });
  },
  deactivate() {
    theme?.dispose();
    dispose?.();
  },
});
