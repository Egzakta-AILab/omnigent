"""Versioned Studio package handlers installed by the server operator."""

import importlib.metadata
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException
from packaging.utils import canonicalize_name

from omnigent.extensions import ExtensionPluginState

STUDIO_RPC_GROUP = "omnigent.studio_rpc.v1"

# These are the only distribution-owned RPCs exposed through the v1 host.
_OWNERS = {
    ("inbox", "setAway"): ("egzakta.comms", "egzakta-comms"),
    ("inbox", "teamView"): ("egzakta.comms", "egzakta-comms"),
    ("releases", "getCandidate"): ("egzakta.release", "egzakta-release"),
    ("releases", "recordScenario"): ("egzakta.release", "egzakta-release"),
    ("releases", "previewIssue"): ("egzakta.release", "egzakta-release"),
    ("releases", "confirmIssue"): ("egzakta.release", "egzakta-release"),
    ("releases", "createIssueConfirmed"): ("egzakta.release", "egzakta-release"),
    ("releases", "signOff"): ("egzakta.release", "egzakta-release"),
}


@dataclass(frozen=True)
class StudioRpcContext:
    """Trusted server-side context. Package writes must include the workspace key."""

    actor: str
    workspace_id: int
    storage_location: str
    actor_generation: str | None


class StudioRpcHandler(Protocol):
    """The installed entry point is a zero-argument factory returning this object."""

    def invoke(
        self, method: str, params: dict[str, Any], context: StudioRpcContext
    ) -> dict[str, Any]: ...

    def can_sign_off(self, candidate_id: str, context: StudioRpcContext) -> bool:
        """Read the candidate's QA / release-owner grants from authoritative storage."""
        ...


def studio_package_method(scope: str, method: str) -> bool:
    return (scope, method) in _OWNERS


def load_studio_handlers(
    extensions: ExtensionPluginState,
    entry_points: Iterable[importlib.metadata.EntryPoint] | None = None,
) -> dict[str, StudioRpcHandler]:
    """Bind each method to its configured installed wheel and validated manifest."""
    selected: dict[str, StudioRpcHandler] = {}
    installed = tuple(
        importlib.metadata.entry_points(group=STUDIO_RPC_GROUP)
        if entry_points is None
        else entry_points
    )
    for owner, distribution in sorted(set(_OWNERS.values())):
        manifest = extensions.get(owner)
        if not manifest or canonicalize_name(manifest.distribution) != distribution:
            continue
        matching = [
            entry
            for entry in installed
            if entry.name == owner
            and entry.dist
            and canonicalize_name(entry.dist.name) == distribution
        ]
        if len(matching) == 1:
            try:
                factory = matching[0].load()
                handler = factory()
                if callable(getattr(handler, "invoke", None)) and (
                    owner != "egzakta.release" or callable(getattr(handler, "can_sign_off", None))
                ):
                    selected[owner] = handler
            except Exception:
                logging.getLogger(__name__).exception(
                    "Studio RPC handler unavailable for %s", owner
                )
    return selected


def invoke_studio_handler(
    handlers: Mapping[str, StudioRpcHandler],
    scope: str,
    method: str,
    params: dict[str, Any],
    context: StudioRpcContext,
) -> dict[str, Any]:
    owner, _ = _OWNERS[(scope, method)]
    handler = handlers.get(owner)
    if handler is None:
        raise HTTPException(
            503, {"status": "env_blocked", "reason": f"The {owner} package handler is unavailable"}
        )
    if scope == "releases":
        candidate = params.get("candidate_id")
        if not isinstance(candidate, str) or not 1 <= len(candidate.strip()) <= 128:
            raise HTTPException(422, "A candidate ID is required")
        if method == "signOff" and not handler.can_sign_off(candidate, context):
            raise HTTPException(403, "Candidate sign-off requires its QA or release owner")
    return handler.invoke(method, params, context)
