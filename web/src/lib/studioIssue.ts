export const STUDIO_ISSUE_LABEL = "omnigent.studio.issue";

export function isImplementationSession(labels: Record<string, string>, issue: string): boolean {
  return labels[STUDIO_ISSUE_LABEL] === issue && labels["omnigent.studio.review"] !== "true";
}

export function linearIssueKey(input: string): string | null {
  let value = input.trim();
  if (value.startsWith("https://")) {
    try {
      const url = new URL(value);
      if (url.hostname !== "linear.app" || url.port || url.username || url.password) return null;
      value = url.pathname.match(/^\/[^/]+\/issue\/([^/]+)(?:\/|$)/)?.[1] ?? "";
    } catch {
      return null;
    }
  }
  return /^[A-Z][A-Z0-9]{0,11}-[1-9][0-9]{0,11}$/i.test(value) ? value.toUpperCase() : null;
}

export function issueWorkspaceUrl(project: string, issue?: string, review?: string): string {
  const query = new URLSearchParams({ project });
  if (issue) query.set("issue", issue);
  if (review) query.set("review", review);
  return `/work?${query}`;
}
