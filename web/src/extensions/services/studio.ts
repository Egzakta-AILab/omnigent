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
  allowExternal?: (url: string) => void,
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
  for (const name of [
    "list",
    "getOrCreate",
    "updateAcceptance",
    "linkPr",
    "linkSession",
  ] as const) {
    methods[`studio.tasks.${name}`] = async (params, signal) => {
      const result = object(await post(`tasks/${name}`, params, signal));
      for (const task of Array.isArray(result.items) ? result.items : [result]) {
        for (const pr of Array.isArray(task.pull_requests) ? task.pull_requests : []) {
          if (
            typeof pr.repository === "string" &&
            /^[a-z0-9_-][a-z0-9_.-]*\/[a-z0-9_-][a-z0-9_.-]*$/i.test(pr.repository) &&
            Number.isSafeInteger(pr.number) &&
            pr.number > 0
          ) {
            allowExternal?.(`https://github.com/${pr.repository}/pull/${pr.number}`);
          }
        }
      }
      return result;
    };
  }
  methods["studio.fleet.inventory"] = async (params, signal) => {
    const result = object(await post("fleet/inventory", params, signal));
    for (const preview of Array.isArray(result.previews) ? result.previews : []) {
      if (
        typeof preview.url === "string" &&
        preview.url.startsWith("https://") &&
        /^[0-9a-f]{40}$/.test(preview.requested_sha) &&
        preview.requested_sha === preview.checked_out_sha
      )
        allowExternal?.(preview.url);
    }
    return result;
  };
  for (const name of ["getWalkthrough", "askFrozen", "dispute", "dismiss"] as const) {
    methods[`studio.reviews.${name}`] = async (params, signal) => {
      const result = object(await post(`reviews/${name}`, params, signal));
      for (const list of [result.walkthrough, result.proofs, result.citations]) {
        if (!Array.isArray(list)) continue;
        for (const item of list) {
          for (const url of [item?.url, item?.services_url]) {
            if (
              typeof url === "string" &&
              /^https:\/\/github\.com\/Egzakta-AILab\/(?:KVARK|Services)\/blob\/[0-9a-f]{40}\/[^?#]+#L[1-9]\d*$/.test(
                url,
              )
            ) {
              allowExternal?.(url);
            }
          }
        }
      }
      return result;
    };
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
