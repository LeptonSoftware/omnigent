#!/usr/bin/env bash
# Fork: bring upstream release tags into this checkout when it lacks them.
#
# The fork publishes no release tags of its own (a pushed v* tag would run the
# release and image-publish workflows), but the compat and backcompat jobs pin
# their "old" side to upstream releases. Fetching tags into the runner's local
# clone lets `git tag` and `git worktree add <tag>` resolve without publishing.
#
# Usage: fetch-upstream-release-tags.sh          # every v* tag, if none exist
#        fetch-upstream-release-tags.sh TAG...   # just these tags, if missing
set -euo pipefail

upstream="${OMNIGENT_UPSTREAM_REPO_URL:-https://github.com/omnigent-ai/omnigent.git}"

if [ "$#" -eq 0 ]; then
  if [ -n "$(git tag --list 'v[0-9]*' | head -1)" ]; then
    exit 0
  fi
  git fetch --quiet --no-tags "$upstream" '+refs/tags/v*:refs/tags/v*'
  echo "Fetched $(git tag --list 'v[0-9]*' | wc -l) release tags from $upstream" >&2
  exit 0
fi

for tag in "$@"; do
  [ -z "$tag" ] && continue
  if git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null; then
    continue
  fi
  git fetch --quiet --no-tags "$upstream" "+refs/tags/$tag:refs/tags/$tag"
  echo "Fetched $tag from $upstream" >&2
done
