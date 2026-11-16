"""Read-only Linear queries for the separately installed Linear board."""

import json
from typing import Annotated, Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_validator

LINEAR_EXTENSION_ID = "egzakta.linear"
_ENDPOINT = "https://api.linear.app/graphql"
_DIRECTORY = """
query OmnigentLinearDirectory {
  viewer { id name }
  organization { id name }
  teams(first: 50) { nodes { id name } }
}
"""
_ISSUE_PAGE = """
  nodes {
    id identifier title priority
    state { id name }
    project { id name }
    assignee { id name }
  }
  pageInfo { hasNextPage endCursor }
"""
_BOARD = (
    """
query OmnigentLinearBoard($team: String!, $after: String, $filter: IssueFilter) {
  team(id: $team) {
    id
    issues(first: 30, after: $after, filter: $filter) {
"""
    + _ISSUE_PAGE
    + """
    }
  }
}
"""
)
_SEARCH = (
    """
query OmnigentLinearSearch($term: String!, $after: String, $filter: IssueFilter) {
  searchIssues(term: $term, first: 30, after: $after, filter: $filter) {
"""
    + _ISSUE_PAGE
    + """
  }
}
"""
)
_ISSUE = """
query OmnigentLinearIssue($issue: String!) {
  issue(id: $issue) { id identifier title description url }
}
"""


class LinearReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: SecretStr
    team: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")] | None = None
    issue: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")] | None = None
    after: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = None

    search: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    assignee: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")] | None = None

    @field_validator("token")
    @classmethod
    def validate_token(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value()
        if not 12 <= len(value) <= 256 or any(not 33 <= ord(char) <= 126 for char in value):
            raise ValueError("Enter a Linear personal API key")
        return token


async def read_linear(request: LinearReadRequest) -> dict[str, Any]:
    """Run a fixed query with the requesting user's key and no token retention."""
    query, variables = _DIRECTORY, {}
    if request.issue:
        query, variables = _ISSUE, {"issue": request.issue}
    elif request.team:
        filters: dict[str, Any] = {}
        if request.assignee:
            filters["assignee"] = {"id": {"eq": request.assignee}}
        if request.search:
            filters["team"] = {"id": {"eq": request.team}}
            query = _SEARCH
            variables = {"term": request.search, "after": request.after, "filter": filters}
        else:
            query = _BOARD
            variables = {"team": request.team, "after": request.after, "filter": filters}
    try:
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=False) as client,
            client.stream(
                "POST",
                _ENDPOINT,
                headers={"Authorization": request.token.get_secret_value()},
                json={"query": query, "variables": variables},
            ) as response,
        ):
            if response.status_code in (401, 403):
                raise HTTPException(403, "Linear rejected the key or access to this team")
            if response.status_code == 429:
                raise HTTPException(429, "Linear rate limited the request. Try again shortly.")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 512 * 1024:
                    raise HTTPException(502, "Linear returned more data than the board can load")
        result = json.loads(body)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Linear did not return a usable response") from exc
    if (
        not isinstance(result, dict)
        or result.get("errors")
        or not isinstance(result.get("data"), dict)
    ):
        raise HTTPException(502, "Linear could not complete this query")
    searched = request.team and request.search and not request.issue
    resource = (
        "issue"
        if request.issue
        else "searchIssues"
        if searched
        else "team"
        if request.team
        else None
    )
    if resource and not isinstance(result["data"].get(resource), dict):
        raise HTTPException(404, f"This {resource} is unavailable to this Linear account")
    data = result["data"]
    if searched:
        data = {"team": {"issues": data["searchIssues"]}}
    if request.issue and isinstance(description := data["issue"].get("description"), str):
        if len(description) > 8000:
            data["issue"]["description"] = description[:8000] + "\n\n[Description excerpt]"
    if len(json.dumps(data, ensure_ascii=False).encode("utf-16-le")) > 56 * 1024:
        raise HTTPException(502, "Linear returned more data than the board can display")
    return data
