"""Close the native host and session surfaces for centrally credentialed runners."""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection

from omnigent.db.utils import shared_read_scope
from omnigent.entities import Conversation
from omnigent.runner.routing import routing_host_session
from omnigent.server.auth import AuthProvider, effective_admin
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.sandbox_profile_store import SandboxProfileStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

PROVIDER = "egzakta.coder"
POOLED_PROFILES = frozenset(("free-agent", "bot-review"))
DENIAL = "Pooled host access requires an administrator"


def pooled_profile(snapshot: Mapping[str, Any] | None) -> bool:
    return bool(
        snapshot and snapshot.get("provider") == PROVIDER and snapshot.get("id") in POOLED_PROFILES
    )


def pooled_target(
    conversations: ConversationStore,
    hosts: HostStore | None,
    session_id: str | None,
    host_id: str | None,
) -> bool:
    session = conversations.get_conversation(session_id) if session_id else None
    return _pooled_target(conversations, hosts, session_id, host_id, session)


def _pooled_target(
    conversations: ConversationStore,
    hosts: HostStore | None,
    session_id: str | None,
    host_id: str | None,
    session: Conversation | None,
) -> bool:
    if session:
        owner = routing_host_session(session, conversations) or session
        labels = owner.labels or {}
        if (
            labels.get("sandbox_profile:provider") == PROVIDER
            and labels.get("sandbox_profile:id") in POOLED_PROFILES
        ):
            return True
        host_id = host_id or owner.host_id
    if not host_id or hosts is None:
        return False
    host = hosts.get_host(host_id)
    if host is None or host.sandbox_provider != PROVIDER:
        return False
    # Lost/ambiguous provenance is closed. Account ownership alone never makes
    # an unclassified Coder host safe for interactive access.
    snapshot = SandboxProfileStore(conversations).get_host_snapshot(
        host.host_id, reference_session_id=session_id
    )
    return not snapshot or snapshot.get("id") != "dev-personal"


def pooled_access_dependency(
    conversations: ConversationStore,
    hosts: HostStore | None,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
) -> Callable[[HTTPConnection], None]:
    def check(connection: HTTPConnection) -> None:
        path = connection.path_params
        host_id = path.get("host_id")
        session_id = path.get("session_id") or path.get("conversation_id")
        if not host_id and not session_id:
            return
        # The tunnel authenticates a machine with its one-time binding. This
        # connection transports Studio's own runner and is not a shell grant.
        if connection.scope["type"] == "websocket" and connection.url.path.endswith("/tunnel"):
            return
        try:
            # Dynamic URL segments also match static collection routes.
            if session_id:
                uuid_to_bytes(session_id)
            if host_id:
                uuid_to_bytes(host_id)
            session = conversations.get_conversation(session_id) if session_id else None
            closed = _pooled_target(conversations, hosts, session_id, host_id, session)
            # Policy evaluation can reuse this request-local row for its ACL
            # lookup. The routing owner may be a parent; keep the requested row.
            connection.state.studio_checked_conversation = session
        except InvalidUuidError:
            return
        except ValueError:
            closed = True
        if not closed:
            return
        user = auth.get_user_id(connection) if auth else None
        if user and permissions and effective_admin(permissions.is_admin(user)):
            return
        if connection.scope["type"] == "websocket":
            raise WebSocketException(code=1008, reason=DENIAL)
        raise HTTPException(403, DENIAL)

    def check_and_read(connection: HTTPConnection) -> None:
        read_inputs = getattr(connection.scope.get("endpoint"), "session_snapshot_inputs", None)
        if read_inputs is None:
            check(connection)
            return
        with shared_read_scope():
            check(connection)
            connection.state.session_snapshot_inputs = read_inputs(connection)

    return check_and_read
