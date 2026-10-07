#!/usr/bin/env python3
"""Create a GitHub org team and map it to an IdP (team-sync) group.

Uses the `gh` CLI for auth/transport, so `gh auth login` must already be done
for the target host with a token carrying the `admin:org` scope.

What it does, idempotently:
  1. Reuse the team if it already exists, otherwise create it (privacy=closed,
     which GitHub requires for team synchronization).
  2. Resolve the IdP group's numeric group_id from its name (the group-mappings
     API needs the id, not just the name).
  3. Apply the group mapping and print the resulting state.

Examples
--------
    ./create_github_team_with_group_sync.py \
        --org qualcomm \
        --name tps-location-triage \
        --description "Contributors for tps-location repos" \
        --group Contributors.tps-location

    # Just create the team, no group sync:
    ./create_github_team_with_group_sync.py --org qualcomm --name my-team

    # Against GitHub Enterprise Server:
    ./create_github_team_with_group_sync.py --host github.qualcomm.com ...
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any
def gh_api(
    path: str,
    *,
    host: str | None = None,
    method: str | None = None,
    fields: dict[str, str] | None = None,
    input_json: Any | None = None,
) -> Any:
    """Call `gh api <path>` and return parsed JSON (or None on empty body).

    Raises RuntimeError with gh's stderr on non-zero exit.
    """
    cmd = ["gh", "api"]
    if host:
        cmd += ["--hostname", host]
    if method:
        cmd += ["--method", method]
    for key, value in (fields or {}).items():
        cmd += ["-f", f"{key}={value}"]
    if input_json is not None:
        cmd += ["--input", "-"]
    cmd.append(path)

    proc = subprocess.run(
        cmd,
        input=json.dumps(input_json) if input_json is not None else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def get_team(org: str, slug: str, *, host: str | None) -> dict | None:
    """Return the team dict if it exists, else None."""
    try:
        return gh_api(f"orgs/{org}/teams/{slug}", host=host)
    except RuntimeError as exc:
        if "Not Found" in str(exc) or "404" in str(exc):
            return None
        raise


def ensure_team(org: str, name: str, description: str, *, host: str | None) -> dict:
    """Create the team, or reuse it if a team with the same slug already exists.

    GitHub derives the slug from the name; we look it up by the lowercased,
    dash-joined form which matches GitHub's slugging for simple names. If your
    name has unusual characters, pass the real slug via the returned dict.
    """
    slug = name.strip().lower().replace(" ", "-")
    existing = get_team(org, slug, host=host)
    if existing:
        print(f"↺ team '{slug}' already exists (id {existing['id']}); reusing it.")
        return existing

    # privacy=closed is mandatory for team synchronization.
    team = gh_api(
        f"orgs/{org}/teams",
        host=host,
        method="POST",
        fields={"name": name, "description": description, "privacy": "closed"},
    )
    print(f"✓ created team '{team['slug']}' (id {team['id']}) → {team['html_url']}")
    return team


def resolve_group(org: str, group_name: str, *, host: str | None) -> dict:
    """Find an IdP group by exact name using the endpoint's `q=` filter.

    The group list can be huge (cursor-paginated), so we rely on server-side
    filtering rather than walking every page. Raises if no exact match.
    """
    resp = gh_api(
        f"orgs/{org}/team-sync/groups?per_page=100&q={group_name}", host=host
    )
    groups = (resp or {}).get("groups", [])
    exact = [g for g in groups if g.get("group_name") == group_name]
    if not exact:
        near = ", ".join(g.get("group_name", "?") for g in groups[:10]) or "(none)"
        raise SystemExit(
            f"✗ no IdP group exactly named '{group_name}'. "
            f"Closest matches from q-filter: {near}"
        )
    if len(exact) > 1:
        ids = ", ".join(g["group_id"] for g in exact)
        raise SystemExit(f"✗ multiple groups named '{group_name}' (ids: {ids}).")
    group = exact[0]
    print(
        f"✓ resolved group '{group['group_name']}' → {group['group_id']}"
        + (f"  ({group['group_description']})" if group.get("group_description") else "")
    )
    return group


def map_group(org: str, slug: str, group: dict, *, host: str | None) -> Any:
    """PATCH the team's group-mappings to the single given group."""
    body = {
        "groups": [
            {
                "group_id": group["group_id"],
                "group_name": group["group_name"],
                "group_description": group.get("group_description", ""),
            }
        ]
    }
    result = gh_api(
        f"orgs/{org}/teams/{slug}/team-sync/group-mappings",
        host=host,
        method="PATCH",
        input_json=body,
    )
    print("✓ applied group mapping:")
    for g in (result or {}).get("groups", []):
        print(f"    {g['group_name']} ({g['group_id']}) status={g.get('status')}")
    print(
        "  Note: status 'unsynced' right after a PATCH is normal; GitHub's sync "
        "job populates membership on its next run."
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a GitHub org team and map it to an IdP group via gh api."
    )
    parser.add_argument("--org", required=True, help="Organization login, e.g. qualcomm")
    parser.add_argument("--name", required=True, help="Team name")
    parser.add_argument(
        "--description", default="", help="Team description (optional)"
    )
    parser.add_argument(
        "--group",
        help="Exact IdP group name to map (omit to only create the team)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="GitHub host (e.g. github.qualcomm.com). Omit for github.com.",
    )
    args = parser.parse_args()

    try:
        team = ensure_team(args.org, args.name, args.description, host=args.host)
        if args.group:
            group = resolve_group(args.org, args.group, host=args.host)
            map_group(args.org, team["slug"], group, host=args.host)
        else:
            print("(no --group given; skipping group sync)")
    except (RuntimeError, SystemExit) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
