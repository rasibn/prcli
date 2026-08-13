#!/usr/bin/env python3
"""Sync GitHub labels on my open PRs with their live state, then dump a report.

State labels managed by this script (added/removed automatically):
  - "conflicts"            mergeable == CONFLICTING
  - "outdated"             head branch is behind its base branch
  - "unresolved comments"  open (unresolved) review threads exist
  - "size: small|medium|large"  additions+deletions: <120 / 120-400 / >400

Manual labels (read-only here, set by the user): P0 - Immediate, P1 - High,
P3 - Low, P4 - Lowest,
need qa testing, ready to release.

Usage: sync_prs.py [--repo owner/name] [--dry-run] [--json]
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

DEFAULT_REPO = "careem/bike-experience-service"

L_CONFLICTS = "conflicts"
L_OUTDATED = "outdated"
L_UNRESOLVED = "unresolved comments"
SIZES = ["size: small", "size: medium", "size: large"]
L_QA_NEEDED = "need qa testing"
L_QA_DONE = "ready to release"
PRIORITIES = ["P0 - Immediate", "P1 - High", "P3 - Low", "P4 - Lowest"]
KNOWN_LABELS = [
    L_CONFLICTS,
    L_OUTDATED,
    L_UNRESOLVED,
    *SIZES,
    *PRIORITIES,
    L_QA_NEEDED,
    L_QA_DONE,
]


def ensure_labels(repo: str, labels: list[str]) -> None:
    """Create any requested repository labels that do not exist yet."""
    if not labels:
        return
    existing = {
        label["name"]
        for label in json.loads(gh("label", "list", "--repo", repo, "--limit", "200", "--json", "name"))
    }
    for label in labels:
        if label not in existing:
            gh("label", "create", label, "--repo", repo, "--force")


def gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return proc.stdout


def enrich(repo: str, pr: dict) -> None:
    owner, name = repo.split("/")
    # commits the head branch is behind its base
    try:
        pr["behind_by"] = int(
            gh(
                "api",
                f"repos/{repo}/compare/{pr['headRefName']}...{pr['baseRefName']}",
                "-q",
                ".ahead_by",
            ).strip()
        )
    except Exception:
        pr["behind_by"] = None
    # unresolved review threads
    try:
        out = gh(
            "api",
            "graphql",
            "-f",
            "query=query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}}}}}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={pr['number']}",
        )
        nodes = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"][
            "nodes"
        ]
        pr["unresolved"] = sum(1 for n in nodes if not n["isResolved"])
    except Exception:
        pr["unresolved"] = None


def size_label(pr: dict) -> str:
    lines = pr["additions"] + pr["deletions"]
    if lines < 120:
        return "size: small"
    if lines <= 400:
        return "size: medium"
    return "size: large"


def desired_state_labels(pr: dict) -> dict:
    """Map of state label -> True (should have) / False (should not) / None (unknown, leave as-is)."""
    conflicting = None
    if pr["mergeable"] == "CONFLICTING":
        conflicting = True
    elif pr["mergeable"] == "MERGEABLE":
        conflicting = False
    state = {
        L_CONFLICTS: conflicting,
        L_OUTDATED: pr["behind_by"] > 0 if pr["behind_by"] is not None else None,
        L_UNRESOLVED: pr["unresolved"] > 0 if pr["unresolved"] is not None else None,
    }
    want = size_label(pr)
    for s in SIZES:
        state[s] = s == want
    return state


def edit_labels(repo: str, number: int, add: list[str], remove: list[str]) -> None:
    cmd = ["pr", "edit", str(number), "--repo", repo]
    for l in add:
        cmd += ["--add-label", l]
    for l in remove:
        cmd += ["--remove-label", l]
    gh(*cmd)


def rebase_update(node_id: str) -> None:
    gh(
        "api",
        "graphql",
        "-f",
        "query=mutation($id: ID!) { updatePullRequestBranch(input: {pullRequestId: $id, updateMethod: REBASE}) { pullRequest { number } } }",
        "-f",
        f"id={node_id}",
    )


def sync(repo: str = DEFAULT_REPO, dry_run: bool = False) -> list[dict]:
    """Fetch my open PRs, sync state labels (unless dry_run), return report rows."""
    if not dry_run:
        ensure_labels(repo, KNOWN_LABELS)
    prs = json.loads(
        gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--author",
            "@me",
            "--limit",
            "100",
            "--json",
            "id,number,title,url,labels,mergeable,mergeStateStatus,baseRefName,headRefName,isDraft,additions,deletions",
        )
    )
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda p: enrich(repo, p), prs))

    report = []
    for pr in prs:
        labels = {l["name"] for l in pr["labels"]}
        add, remove = [], []
        for label, want in desired_state_labels(pr).items():
            if want is True and label not in labels:
                add.append(label)
            elif want is False and label in labels:
                remove.append(label)
        if (add or remove) and not dry_run:
            cmd = ["pr", "edit", str(pr["number"]), "--repo", repo]
            for l in add:
                cmd += ["--add-label", l]
            for l in remove:
                cmd += ["--remove-label", l]
            gh(*cmd)
            labels = (labels | set(add)) - set(remove)

        report.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "branch": pr["headRefName"],
                "node_id": pr["id"],
                "draft": pr["isDraft"],
                "base": pr["baseRefName"],
                "size": next((s for s in SIZES if s in labels), None),
                "lines_changed": pr["additions"] + pr["deletions"],
                "priority": next((p for p in PRIORITIES if p in labels), None),
                "qa_lane": "ready to release"
                if L_QA_DONE in labels
                else ("needs qa" if L_QA_NEEDED in labels else "untracked"),
                "conflicts": pr["mergeable"] == "CONFLICTING",
                "mergeable": pr["mergeable"],
                "behind_by": pr["behind_by"],
                "unresolved_comments": pr["unresolved"],
                "labels_added": add,
                "labels_removed": remove,
                "labels": sorted(labels),
            }
        )
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument(
        "--dry-run", action="store_true", help="report changes without applying them"
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    report = sync(args.repo, dry_run=args.dry_run)

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return

    for r in report:
        flags = []
        if r["conflicts"]:
            flags.append("CONFLICTS")
        if r["behind_by"]:
            flags.append(f"BEHIND x{r['behind_by']}")
        if r["unresolved_comments"]:
            flags.append(f"UNRESOLVED x{r['unresolved_comments']}")
        if r["draft"]:
            flags.append("draft")
        changes = ""
        if r["labels_added"] or r["labels_removed"]:
            changes = f"  [labels: +{r['labels_added']} -{r['labels_removed']}]"
        print(f"#{r['number']} {' '.join(flags) or 'clean'}{changes}  {r['title']}")


if __name__ == "__main__":
    main()
