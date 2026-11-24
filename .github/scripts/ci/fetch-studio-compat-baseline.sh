#!/usr/bin/env bash
# Make the fork's upstream compatibility baseline available to Git worktrees.
# CI checkouts run in separate jobs. Only their local tag namespace is updated.
set -euo pipefail
tag=v0.23.0
commit=033963227a3b54803ae00c5ef0f0f6fe97f5549d
if ! git show-ref --verify --quiet "refs/tags/$tag"; then
  if ! git cat-file -e "$commit^{commit}" 2>/dev/null; then
    git fetch --no-tags --depth=1 https://github.com/omnigent-ai/omnigent.git "$commit"
  fi
  git tag "$tag" "$commit"
fi
[[ $(git rev-parse "refs/tags/$tag^{commit}") == "$commit" ]] || {
  echo "Local $tag does not match the reviewed baseline $commit" >&2
  exit 1
}
