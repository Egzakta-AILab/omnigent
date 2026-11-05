import { useMemo, useState } from "react";
import { useExtensions } from "@/extensions/ExtensionProvider";
import { ExtensionViewHost } from "@/extensions/ExtensionViewHost";
import type { ExtensionCatalogItem } from "@/extensions/types";
import { createReviewPanelMethods, type ReviewProofTarget } from "./creatorReviewBridge";

const pinnedBundle = async () => [];

function CreatorReviewPanel({
  extension,
  contribution,
  runId,
  onProof,
}: {
  extension: ExtensionCatalogItem;
  contribution: NonNullable<ExtensionCatalogItem["review_panel"]>;
  runId: string;
  onProof: (target: ReviewProofTarget, attach: boolean) => boolean;
}) {
  const [open, setOpen] = useState(false);
  const page = useMemo(
    () => ({
      id: `${extension.id}.review-panel`,
      route: "",
      title: contribution.title,
      view: contribution.view,
    }),
    [extension.id, contribution],
  );
  const methods = useMemo(
    () =>
      createReviewPanelMethods(runId, contribution.capabilities, {
        openProof: (target) => onProof(target, false),
        attachProof: (target) => onProof(target, true),
      }),
    [runId, contribution, onProof],
  );
  return (
    <details
      className="min-w-0 rounded-md border"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">{page.title}</summary>
      {open && (
        <div className="h-80 border-t">
          <ExtensionViewHost
            extension={extension}
            page={page}
            refresh={pinnedBundle}
            methods={methods}
            embedded
          />
        </div>
      )}
    </details>
  );
}

export function CreatorReviewPanels(props: {
  runId: string;
  onProof: (target: ReviewProofTarget, attach: boolean) => boolean;
}) {
  return useExtensions().map((extension) => {
    const contribution = extension.review_panel;
    if (
      extension.status !== "enabled" ||
      !extension.browser.digest ||
      contribution?.slot !== "review.panel" ||
      contribution.contract_version !== "1" ||
      !Array.isArray(contribution.capabilities)
    ) {
      return null;
    }
    return (
      <CreatorReviewPanel
        key={JSON.stringify([extension.id, extension.browser.digest, contribution])}
        extension={extension}
        contribution={contribution}
        {...props}
      />
    );
  });
}
