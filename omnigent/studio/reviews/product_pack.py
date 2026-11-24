"""Capture the review policy and changed paths from independently verified GitHub revisions."""

from __future__ import annotations

import base64
import fnmatch
import os
import re
from typing import Any, Literal
from urllib.parse import quote

import httpx
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from omnigent.studio.reviews.report import ReviewFinding

SHA = r"^[0-9a-f]{40}$"
REPOSITORY = "Egzakta-AILab/KVARK"
CATALOG_REPOSITORY = "Egzakta-AILab/egzakta-org"
LENSES = ("backend", "frontend", "ai-quality", "cicd", "security")


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["studio-review-trigger@1"]
    repository: Literal["Egzakta-AILab/KVARK"]
    pull_number: int = Field(gt=0)
    base_ref: str = Field(min_length=1, max_length=255)
    base_sha: str = Field(pattern=SHA)
    head_sha: str = Field(pattern=SHA)
    mode: Literal["delta", "full"]


def unavailable(reason: str) -> HTTPException:
    return HTTPException(503, {"status": "env_blocked", "reason": reason})


class GitHubCapture:
    """The URL authority, repositories and transport credentials are server-controlled."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def read(self, repository: str, endpoint: str, **params: str | int) -> Any:
        response = self.client.get(f"/repos/{repository}/{endpoint}", params=params)
        if response.status_code != 200:
            raise unavailable("GitHub could not provide the complete pinned review context")
        return response.json()

    def file(self, repository: str, path: str, sha: str) -> str:
        path = ReviewFinding.repository_relative_path(path)
        if not re.fullmatch(SHA, sha):
            raise ValueError("Context requires a full commit SHA")
        entry = self.read(repository, f"contents/{quote(path, safe='/')}", ref=sha)
        if entry["type"] != "file" or entry["encoding"] != "base64" or entry["path"] != path:
            raise ValueError("Pinned review input must be a regular file")
        data = base64.b64decode("".join(entry["content"].split()), validate=True)
        if not data or len(data) != entry["size"] or len(data) > 512 * 1024:
            raise ValueError("Incomplete or oversized pinned review file")
        return data.decode()

    def pull(self, request: Trigger) -> dict[str, Any]:
        pr = self.read(request.repository, f"pulls/{request.pull_number}")
        if (
            pr["number"] != request.pull_number
            or pr["state"] != "open"
            or not pr["head"]["repo"]
            or pr["base"]["repo"]["full_name"] != request.repository
            or pr["base"]["ref"] != request.base_ref
            or pr["base"]["sha"] != request.base_sha
            or pr["head"]["sha"] != request.head_sha
            or (
                pr["draft"] is not False
                and not any(x["name"] == "studio:review" for x in pr["labels"])
            )
        ):
            raise HTTPException(409, "Review is stale or the pull request is ineligible")
        return pr

    def capture(self, request: Trigger, services_sha: str | None) -> dict[str, Any]:
        pr = self.pull(request)
        policy = yaml.safe_load(self.file(request.repository, "studio.yaml", request.base_sha))
        if (
            policy["schema_version"] != "studio-product-pack@1"
            or policy["repository"] != request.repository
            or policy["review_instructions"] != "REVIEW.md"
            or policy["lenses"]["repository"] != CATALOG_REPOSITORY
            or tuple(policy["lenses"]["profiles"]) != LENSES
            or set(policy["path_map"]) != set(LENSES)
        ):
            raise ValueError("Unsupported Product Pack")
        instructions = self.file(request.repository, "REVIEW.md", request.base_sha)
        lenses = policy["lenses"]
        catalog = yaml.safe_load(
            self.file(CATALOG_REPOSITORY, lenses["catalog"], lenses["revision"])
        )
        count = pr["changed_files"]
        if type(count) is not int or not 0 < count <= 3000:
            raise unavailable("GitHub cannot enumerate the entire pull request")
        comparison = self.read(
            request.repository, f"compare/{request.base_sha}...{request.head_sha}", per_page=1
        )
        files = comparison["files"]
        if count > 300 and len(files) == 300:
            # GitHub limits compare JSON to 300 files. Immutable trees still provide every
            # changed path for lens/protected-path routing, even when hunk proof is unavailable.
            def tree(commit: str) -> dict[str, tuple[str, str]]:
                if not re.fullmatch(SHA, commit):
                    raise ValueError("Invalid immutable tree ref")
                data = self.read(request.repository, f"git/trees/{commit}", recursive=1)
                if data.get("truncated") is not False:
                    raise unavailable("Incomplete immutable Git tree")
                return {
                    item["path"]: (item["sha"], item["mode"])
                    for item in data["tree"]
                    if item["type"] != "tree"
                }

            before = tree(comparison["merge_base_commit"]["sha"])
            after = tree(request.head_sha)
            known = {f[k] for f in files for k in ("filename", "previous_filename") if k in f}
            for path in sorted((before.keys() | after.keys()) - known):
                if before.get(path) != after.get(path):
                    files.append(
                        {
                            "filename": path,
                            "status": "modified",
                            "additions": 0,
                            "deletions": 0,
                            "evidence_gap": True,
                        }
                    )
        if len(files) < count or len({item["filename"] for item in files}) != len(files):
            raise unavailable("Incomplete immutable change list")
        paths = sorted(
            {
                ReviewFinding.repository_relative_path(item[key])
                for item in files
                for key in ("filename", "previous_filename")
                if key in item
            }
        )

        def matches(path: str, patterns: list[str]) -> bool:
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                raise ValueError("Invalid base policy path patterns")
            return any(fnmatch.fnmatchcase(path.lower(), pattern.lower()) for pattern in patterns)

        selected = [
            lens
            for lens in LENSES
            if any(matches(path, policy["path_map"][lens]) for path in paths)
        ]
        protected = [p for p in paths if matches(p, policy["protected_paths"])]
        # A pack can supply component path globs until the graph snapshot is available.
        # An unmapped path (or an absent footprint) cannot qualify for the auto lane.
        mapped = [
            {
                key
                for key, globs in policy.get("component_paths", {}).items()
                if matches(path, globs)
            }
            for path in paths
        ]
        component_count = len(set().union(*mapped)) if mapped and all(mapped) else None
        profiles = {
            lens: self.file(
                CATALOG_REPOSITORY, catalog["profiles"][lens]["file"], lenses["revision"]
            )
            for lens in selected
        }
        services: dict[str, str] = {}
        if services_sha:
            for entry in policy["context"]:
                if (entry["repository"], entry["access"], entry["revision_source"]) != (
                    "Egzakta-AILab/Services",
                    "read_only",
                    "deployed_sha",
                ):
                    raise ValueError("Invalid Services context declaration")
                for path in entry["files"]:
                    services[path] = self.file(entry["repository"], path, services_sha)
        # Recheck eligibility after immutable policy and evidence collection.
        self.pull(request)
        return {
            "selected_lenses": selected,
            "protected_paths": protected,
            "component_count": component_count,
            "profiles": profiles,
            "instructions": instructions,
            "policy": policy,
            "files": files,
            "author_login": pr["user"]["login"],
            "asked_logins": [r["login"] for r in pr["requested_reviewers"]],
            "labels": [r["name"] for r in pr["labels"]],
            "head_ref": pr["head"]["ref"],
            "services_revision": services_sha,
            "services": services,
        }


def github_gate_facts(client: httpx.Client, trigger: Trigger) -> dict[str, Any]:
    """Advisory facts for the auto lane. Loss of auxiliary GitHub access leaves it closed."""
    query = """query($owner:String!,$repo:String!,$n:Int!) {
      repository(owner:$owner,name:$repo){pullRequest(number:$n){
        headRefOid baseRefOid mergeStateStatus commits(last:100) {
          totalCount nodes { commit { authors(first:10) { totalCount nodes { user { login } } }
            statusCheckRollup { state } oid } }
        }
      }}
    }"""
    owner, repository = trigger.repository.split("/")
    try:
        result = client.post(
            "/graphql",
            json={
                "query": query,
                "variables": {"owner": owner, "repo": repository, "n": trigger.pull_number},
            },
        )
        result.raise_for_status()
        pr = result.json()["data"]["repository"]["pullRequest"]
        if pr["headRefOid"] != trigger.head_sha or pr["baseRefOid"] != trigger.base_sha:
            raise HTTPException(409, "Pull request changed before review admission")
        commits = pr["commits"]["nodes"]
        complete = 0 < len(commits) == pr["commits"]["totalCount"]
        head = next(c["commit"] for c in commits if c["commit"]["oid"] == trigger.head_sha)
        bots = {"studio-bot", "github-actions[bot]"}

        def human_commit(commit: dict[str, Any]) -> bool:
            authors = commit["authors"]
            return 0 < len(authors["nodes"]) == authors["totalCount"] and all(
                a["user"]
                and a["user"]["login"].lower() not in bots
                and not a["user"]["login"].lower().endswith("[bot]")
                for a in authors["nodes"]
            )

        return {
            "checks_green": pr["mergeStateStatus"] == "CLEAN"
            and (head["statusCheckRollup"] or {}).get("state") == "SUCCESS",
            "bot_fix": not complete or not all(human_commit(c["commit"]) for c in commits),
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError, StopIteration):
        return {"checks_green": False, "bot_fix": True}


def capture_configured(request: Trigger) -> dict[str, Any]:
    allowed = os.environ.get("OMNIGENT_STUDIO_REVIEW_BASE_REFS", "main").split(",")
    if request.base_ref not in allowed:
        raise HTTPException(403, "The target branch is outside the configured review policy")
    token = os.environ.get("OMNIGENT_STUDIO_GITHUB_READ_TOKEN")
    if not token:
        raise unavailable("GitHub read access is unavailable")
    with httpx.Client(
        base_url="https://api.github.com",
        follow_redirects=False,
        trust_env=False,
        timeout=20,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    ) as client:
        try:
            captured = GitHubCapture(client).capture(
                request, os.environ.get("OMNIGENT_STUDIO_SERVICES_DEPLOYED_SHA")
            )
            return {**captured, **github_gate_facts(client, request)}
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            yaml.YAMLError,
        ) as exc:
            raise unavailable(
                "The base policy or immutable GitHub evidence is unavailable"
            ) from exc
