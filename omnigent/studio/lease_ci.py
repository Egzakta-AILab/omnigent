"""Read authoritative integration evidence without dispatching any CI workflow."""

import os
import re

import httpx

REPO = "Egzakta-AILab/KVARK"
JOB = "Backend / Integration and NATS tests"


def trusted_integration(sha: str, linked: list[dict], client: httpx.Client) -> str | None:
    """Require a task-pinned commit and the existing default-branch nightly job."""
    if not re.fullmatch("[0-9a-f]{40}", sha) or not any(
        p["repository"].casefold() == REPO.casefold() and p["head_sha"] == sha for p in linked
    ):
        return None
    root = "/repos/" + REPO
    try:
        branch = client.get(root).raise_for_status().json()["default_branch"]
        rows = (
            client.get(
                root + "/actions/workflows/nightly.yml/runs",
                params={
                    "head_sha": sha,
                    "branch": branch,
                    "per_page": 10,
                },
            )
            .raise_for_status()
            .json()["workflow_runs"]
        )
        for run in sorted(rows, key=lambda row: int(row["id"]), reverse=True):
            if (
                run["head_sha"] != sha
                or run["head_branch"] != branch
                or run["path"] != ".github/workflows/nightly.yml"
                or run["head_repository"]["full_name"].casefold() != REPO.casefold()
                or run["event"] not in ("schedule", "workflow_dispatch")
            ):
                continue
            if run["status"] != "completed" or run["conclusion"] != "success":
                return None
            rid = int(run["id"])
            attempt = int(run["run_attempt"])
            jobs = (
                client.get(
                    f"{root}/actions/runs/{rid}/attempts/{attempt}/jobs", params={"per_page": 100}
                )
                .raise_for_status()
                .json()["jobs"]
            )
            for job in jobs:
                if (
                    job["name"] == JOB
                    and job["head_sha"] == sha
                    and job["conclusion"] == "success"
                    and any(
                        step["name"] == "Integration and NATS tests"
                        and step["conclusion"] == "success"
                        for step in job["steps"]
                    )
                ):
                    return f"https://github.com/{REPO}/actions/runs/{rid}/job/{int(job['id'])}"
            return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return None


def completed_integration(sha: str, linked: list[dict]) -> str | None:
    token = os.getenv("OMNIGENT_STUDIO_GITHUB_READ_TOKEN")
    if not token:
        return None
    with httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=2,
    ) as client:
        return trusted_integration(sha, linked, client)
