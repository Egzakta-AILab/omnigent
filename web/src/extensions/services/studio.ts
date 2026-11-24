import {
  STUDIO_METHOD_PERMISSIONS,
  type StudioMethod,
} from "../../../../sdks/web-extension/src/studio";
import { ExtensionHostServiceError } from "./errors";

export interface LinearPreview {
  linear_workspace: string;
  linear_issue_id: string;
  body: string;
  change?: "status" | "assignee";
  value?: string;
}
export type ConfirmLinear = (preview: LinearPreview, signal: AbortSignal) => Promise<boolean>;

type HostMethod = (params: unknown, signal: AbortSignal) => Promise<unknown>;
type StudioMethods = { [Method in StudioMethod]: HostMethod };

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ExtensionHostServiceError("InvalidParams", "Studio expects an object");
  }
  return value as Record<string, unknown>;
}

function requiredString(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new ExtensionHostServiceError("InvalidParams", "Missing required Studio value");
  }
  return value;
}

export function studioHostMethods(
  extensionId: string,
  fetch: typeof globalThis.fetch,
  confirm: ConfirmLinear,
): StudioMethods {
  const base = `/v1/extensions/${encodeURIComponent(extensionId)}/studio/v1/`;
  async function post(path: string, params: unknown, signal: AbortSignal): Promise<unknown> {
    signal.throwIfAborted();
    const response = await fetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(object(params)),
      signal,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      const detail = error?.detail;
      if (detail?.status === "env_blocked" && typeof detail.reason === "string") {
        throw new ExtensionHostServiceError("env_blocked", detail.reason);
      }
      const code = response.status === 403 ? "PermissionDenied" : "HostError";
      throw new ExtensionHostServiceError(
        code,
        typeof error?.detail === "string"
          ? error.detail
          : `Studio request failed (${response.status})`,
      );
    }
    return response.json();
  }

  const methods = {} as StudioMethods;
  for (const name of Object.keys(STUDIO_METHOD_PERMISSIONS) as StudioMethod[]) {
    const [, scope, method] = name.split(".");
    methods[name] = (params, signal) => post(`${scope}/${method}`, params, signal);
  }
  methods["studio.tasks.syncLinearConfirmed"] = async (params, signal) => {
    const input = object(params);
    const task_id = requiredString(input.task_id);
    const request_id = requiredString(input.request_id);
    const changeFields = (item: Record<string, unknown>) => {
      if (item.change !== "status" && item.change !== "assignee") {
        throw new ExtensionHostServiceError("InvalidParams", "Unknown Linear change");
      }
      return { change: item.change, value: requiredString(item.value) } as const;
    };
    const operation =
      "change" in input ? changeFields(input) : { body: requiredString(input.body) };
    const raw = object(
      await post("tasks/previewLinear", { task_id, request_id, ...operation }, signal),
    );
    const preview = {
      linear_workspace: requiredString(raw.linear_workspace),
      linear_issue_id: requiredString(raw.linear_issue_id),
      body: requiredString(raw.body),
      ...("change" in raw ? changeFields(raw) : {}),
    };
    signal.throwIfAborted();
    if (!(await confirm(preview, signal))) {
      throw new ExtensionHostServiceError("Cancelled", "Linear update cancelled");
    }
    const approved = {
      task_id,
      request_id,
      ...(preview.change
        ? { change: preview.change, value: preview.value }
        : { body: preview.body }),
    };
    const grant = object(await post("tasks/confirmLinear", approved, signal));
    return post(
      "tasks/syncLinearConfirmed",
      { ...approved, confirmation_token: requiredString(grant.confirmation_token) },
      signal,
    );
  };
  return methods;
}
