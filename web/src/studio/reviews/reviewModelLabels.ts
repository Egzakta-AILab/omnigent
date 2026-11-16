import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import type { NativeModelOption } from "@/lib/types";

export function reviewModelLabels(models: readonly NativeModelOption[], model: string) {
  const hostDefault = models.find((option) => option.isDefault);
  const selected = model ? models.find((option) => option.id === model) : hostDefault;
  const modelId = selected?.model || selected?.id;
  const modelName = selected?.displayName || modelId;
  const defaultModelName = hostDefault?.displayName || hostDefault?.model || hostDefault?.id;
  const defaultEffort = selected?.defaultReasoningEffort;
  return {
    modelId,
    modelName,
    modelSummary: modelName || model || "Agent chooses model",
    defaultModelLabel: defaultModelName
      ? `${defaultModelName} (host default)`
      : "Use agent’s model setting",
    defaultEffort,
    defaultEffortLabel: defaultEffort
      ? `${normalizeEffortLabel(defaultEffort)} (model default)`
      : "Use agent’s effort setting",
  };
}
