"""Send a confirmed Studio handoff comment with a server-held Linear credential."""

import os

import httpx
from fastapi import HTTPException

from omnigent.studio.rpc import LinearComment, LinearIssueChange

_ENDPOINT = "https://api.linear.app/graphql"
_TARGET = """query StudioCommentTarget($issue: String!) {
 viewer { id }
 organization { id }
 issue(id: $issue) { id }
}"""
_CREATE = """mutation StudioConfirmedComment($input: CommentCreateInput!) {
 commentCreate(input: $input) { success comment { id } }
}"""
_UPDATE = """mutation StudioIssueUpdate($issue: String!, $input: IssueUpdateInput!) {
 issueUpdate(id: $issue, input: $input) { success issue { id state { id } assignee { id } } }
}"""
_EXISTING = """query StudioCommentReceipt($comment: String!) {
 comment(id: $comment) { id body issue { id } user { id } }
}"""


async def write_linear(
    workspace: str, issue: str, intent: LinearComment | LinearIssueChange
) -> dict[str, str]:
    token = os.environ.get("OMNIGENT_STUDIO_LINEAR_TOKEN")
    if not token:
        raise HTTPException(
            503, {"status": "env_blocked", "reason": "Studio Linear credential unavailable"}
        )
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, headers={"Authorization": token}
        ) as client:
            target = await client.post(
                _ENDPOINT, json={"query": _TARGET, "variables": {"issue": issue}}
            )
            target.raise_for_status()
            result = target.json()
            data = result.get("data") or {}
            if (
                result.get("errors")
                or (data.get("organization") or {}).get("id") != workspace
                or (data.get("issue") or {}).get("id") != issue
            ):
                raise HTTPException(
                    403, "Linear workspace or issue does not match the confirmed task"
                )
            author = (data.get("viewer") or {}).get("id")
            if not author:
                raise HTTPException(502, "Linear account identity unavailable")
            if isinstance(intent, LinearIssueChange):
                field = "state" if intent.change == "status" else "assignee"
                value = str(intent.value)
                changed = await client.post(
                    _ENDPOINT,
                    json={
                        "query": _UPDATE,
                        "variables": {"issue": issue, "input": {field + "Id": value}},
                    },
                )
                changed.raise_for_status()
                result = changed.json()
                update = (result.get("data") or {}).get("issueUpdate") or {}
                observed = update.get("issue") or {}
                if (
                    result.get("errors")
                    or not update.get("success")
                    or observed.get("id") != issue
                    or (observed.get(field) or {}).get("id") != value
                ):
                    raise HTTPException(502, "Linear did not acknowledge the confirmed change")
                return {"linear_issue_id": issue, "change": intent.change, "value": value}
            comment_id = str(intent.request_id)
            body = intent.body
            created = await client.post(
                _ENDPOINT,
                json={
                    "query": _CREATE,
                    "variables": {"input": {"id": comment_id, "issueId": issue, "body": body}},
                },
            )
            if created.is_success:
                result = created.json()
                receipt = (result.get("data") or {}).get("commentCreate") or {}
                if (
                    not result.get("errors")
                    and receipt.get("success")
                    and (receipt.get("comment") or {}).get("id") == comment_id
                ):
                    return {"comment_id": comment_id}
            # Deterministic provider IDs make retries after a lost response safe.
            existing = await client.post(
                _ENDPOINT, json={"query": _EXISTING, "variables": {"comment": comment_id}}
            )
            existing.raise_for_status()
            result = existing.json()
            prior = (result.get("data") or {}).get("comment") or {}
            if (
                not result.get("errors")
                and prior.get("id") == comment_id
                and (prior.get("issue") or {}).get("id") == issue
                and (prior.get("user") or {}).get("id") == author
            ):
                if prior.get("body") != body:
                    raise HTTPException(409, "This request already published a different comment")
                return {"comment_id": comment_id}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Linear comment receipt unavailable") from exc
    raise HTTPException(502, "Linear did not acknowledge the confirmed comment")
