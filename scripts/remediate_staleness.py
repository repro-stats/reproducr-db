#!/usr/bin/env python3
"""
reproducr-db staleness remediation agent.

For each stale database entry, calls Claude to:
  1. Fetch the official changelog
  2. Determine the correct action (raise_floor / extend_ceiling / close / no_change)
  3. Draft the corrected JSON entry
  4. Open a PR in reproducr-db for human review

Usage:
    python remediate_staleness.py \
        --stale-file stale_entries.json \
        --entries-dir entries \
        --dry-run false \
        --repo repro-stats/reproducr-db
"""

import argparse
import json
import os
import re
import sys
import datetime
from pathlib import Path

import anthropic
from github import Github, Auth, GithubException

# ── Constants ────────────────────────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048

SYSTEM_PROMPT = """\
You are a maintainer of the reproducr-db breaking-changes database for the R
package reproducr. Your job is to review stale database entries and determine
the correct remediation action.

## Database schema
Each entry is a JSON file:
{
  "pkg":          "dplyr",
  "fn":           "filter",
  "from_version": "0.8.99",   -- exclusive lower bound (last safe version)
  "to_version":   "1.2.9",    -- inclusive upper bound (last risky version)
  "risk":         "medium",   -- "high" | "medium" | "low"
  "description":  "...",
  "reference":    "https://...",
  "added_by":     "ndohpenngit",
  "added_date":   "YYYY-MM-DD"
}

## Staleness types
- stale_ceiling: current CRAN version is above to_version.
  The window may need extending if the breaking change still applies.
- stale_floor: from_version is >= 1 major version behind current.
  The window is too wide; from_version should be raised.

## Your task
For each entry you receive:
1. Use web_search to fetch the official changelog at the reference URL.
2. Verify whether the breaking change still applies in new versions.
3. Choose one action:
   - raise_floor:     stale_floor — raise from_version to the X.Y.99
                      sentinel for the minor series immediately before
                      the breaking change (e.g. breaking change in
                      1.0.0 → set from_version to "0.9.99").
                      Always use the X.Y.99 sentinel pattern — never
                      set from_version to an actual released version.
                      IMPORTANT: before raising, verify that the
                      intermediate minor series actually existed on
                      CRAN. If the package jumped directly (e.g.
                      0.8.x → 1.0.0 with no 0.9.x releases), then
                      raising the sentinel is cosmetic only and
                      no_change is correct. In that case, state
                      explicitly in the rationale that no intermediate
                      series existed and the current sentinel already
                      correctly bounds the window.
   - extend_ceiling:  stale_ceiling — extend to_version to cover the
                      current release series if the change still applies.
   - close:           ONLY if the entire window is archaeologically
                      unreachable (e.g. R 3.x entries). Never close for
                      stale_floor alone.
   - no_change:       The staleness flag is a false positive on inspection.

## Critical rules
- Never rely on memory — always fetch the actual changelog.
- If you cannot determine the correct action with confidence, use no_change
  and explain why in the rationale.
- The corrected_entry must preserve all original fields; only change the
  field(s) relevant to the action.
- added_date in corrected_entry must reflect today's date.

## Response format — IMPORTANT
Your FINAL response must be a JSON object and nothing else.
No prose before or after. No markdown fences. No explanation.
Just the raw JSON object:
{
  "action": "raise_floor|extend_ceiling|close|no_change",
  "rationale": "One sentence citing the specific changelog evidence.",
  "corrected_entry": { ...full corrected entry... }
}
For close or no_change, set corrected_entry to null.
"""


# ── JSON parsing helper ───────────────────────────────────────────────────────

def _parse_json(text: str) -> dict | None:
    """Extract and parse the first JSON object found in text."""
    if not text:
        return None
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    # Try full text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try finding a {...} block
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


# ── Claude call ──────────────────────────────────────────────────────────────

def remediate_entry(client: anthropic.Anthropic, entry: dict) -> dict | None:
    """
    Ask Claude to determine the remediation for one stale entry.
    Returns the parsed remediation dict, or None on failure.
    """
    today = datetime.date.today().isoformat()

    user_message = f"""\
Review this stale database entry and determine the correct remediation.

Entry JSON:
{json.dumps(entry, indent=2)}

Staleness status: {entry['status']}
Gap description:  {entry.get('gap', 'N/A')}
Today's date:     {today}

Steps:
1. Use web_search to fetch the changelog at: \
{entry.get('reference', 'search for ' + entry['pkg'] + ' changelog')}
2. Verify the breaking change and version window.
3. Output your final answer as a raw JSON object only — \
no prose before or after, no markdown fences.
"""

    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    # Search all text blocks for JSON
    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw = "\n".join(text_blocks).strip()
    result = _parse_json(raw)

    # If no JSON found, send a follow-up turn forcing JSON-only output
    if result is None:
        print(f"  ↩ No JSON in initial response — requesting JSON-only follow-up")
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": (
                "You have completed your research. Now output your final answer "
                "as a raw JSON object only — no prose, no markdown fences, "
                "nothing before or after the JSON.\n\n"
                "Required format:\n"
                '{"action": "raise_floor|extend_ceiling|close|no_change", '
                '"rationale": "one sentence citing changelog evidence", '
                '"corrected_entry": { ...full entry... } or null}'
            )
        })
        response2 = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text_blocks2 = [b.text for b in response2.content if b.type == "text"]
        raw2 = "\n".join(text_blocks2).strip()
        result = _parse_json(raw2)

        if result is None:
            print(f"  ✗ Could not parse Claude response after follow-up for "
                  f"{entry['pkg']}::{entry['fn']}")
            print(f"  Raw: {raw2[:400]}")

    return result


# ── File path helpers ─────────────────────────────────────────────────────────

def version_to_dashes(version: str) -> str:
    return version.replace(".", "-")


def entry_filepath(entries_dir: Path, pkg: str, fn: str, from_version: str) -> Path:
    return entries_dir / pkg / f"{pkg}__{fn}__{version_to_dashes(from_version)}.json"


def old_filepath(entries_dir: Path, pkg: str, fn: str, from_version: str) -> Path:
    """Find existing file for this entry (from_version may differ)."""
    pkg_dir = entries_dir / pkg
    if not pkg_dir.exists():
        return None
    pattern = f"{pkg}__{fn}__*.json"
    matches = list(pkg_dir.glob(pattern))
    return matches[0] if matches else None


# ── GitHub helpers ────────────────────────────────────────────────────────────

def create_pr(
    gh: Github,
    repo_name: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    files: list[tuple[str, str]],
    files_to_delete: list[str] = None,
) -> str:
    """Create a branch, commit files, and open a PR. Returns the PR URL."""
    repo = gh.get_repo(repo_name)
    base_sha = repo.get_branch(base).commit.sha

    # Create branch
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
    except GithubException as e:
        if e.status == 422:
            print(f"  Branch {branch} already exists, reusing.")
        else:
            raise

    # Commit each file
    for path, content in files:
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.update_file(
                path=path,
                message=f"chore: update {path}",
                content=content,
                sha=existing.sha,
                branch=branch,
            )
        except GithubException:
            repo.create_file(
                path=path,
                message=f"chore: add {path}",
                content=content,
                branch=branch,
            )

    # Delete old files if needed (e.g. renamed from_version)
    for path in (files_to_delete or []):
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.delete_file(
                path=path,
                message=f"chore: remove old entry {path}",
                sha=existing.sha,
                branch=branch,
            )
        except GithubException:
            pass

    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch,
        base=base,
    )
    return pr.html_url


# ── PR body builder ───────────────────────────────────────────────────────────

def build_pr_body(entry: dict, remediation: dict) -> str:
    action    = remediation["action"]
    rationale = remediation["rationale"]
    corrected = remediation.get("corrected_entry")

    action_labels = {
        "raise_floor":    "Raise `from_version` (stale floor)",
        "extend_ceiling": "Extend `to_version` (stale ceiling)",
        "close":          "Close entry (window archaeologically unreachable)",
        "no_change":      "No change (false positive on inspection)",
    }

    body = f"""\
## What does this PR do?

Auto-generated by the staleness remediation agent.
**Action:** {action_labels.get(action, action)}

## Entry

`{entry['pkg']}::{entry['fn']}`
- Staleness: `{entry['status']}`
- Gap: {entry.get('gap', 'N/A')}
- Reference: {entry.get('reference', 'N/A')}

## Changelog evidence

{rationale}

## Related issues

Part of the weekly staleness remediation run. Update the staleness tracking
issue once this PR is merged.

## Type of change

- [ ] Bug fix
- [ ] New feature
- [x] Database entry correction
- [ ] Documentation
- [ ] Other

## Checklist

- [x] Claim verified against official changelog (by agent)
- [ ] Human reviewer has spot-checked the changelog evidence
- [ ] `validate` CI check passes in `reproducr-db`
- [ ] `sync_db.R` runs cleanly after merge
- [ ] Tracking issue updated
"""
    if corrected:
        body += (
            f"\n## Corrected entry\n\n"
            f"```json\n{json.dumps(corrected, indent=2)}\n```\n"
        )

    return body


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="reproducr-db staleness remediation agent"
    )
    parser.add_argument("--stale-file",  required=True)
    parser.add_argument("--entries-dir", required=True)
    parser.add_argument("--dry-run",     default="false")
    parser.add_argument("--repo",        required=True)
    args = parser.parse_args()

    dry_run     = args.dry_run.lower() == "true"
    entries_dir = Path(args.entries_dir)

    with open(args.stale_file) as f:
        stale_entries = json.load(f)

    if not stale_entries:
        print("No stale entries to remediate.")
        sys.exit(0)

    print(f"Remediating {len(stale_entries)} stale entries "
          f"({'dry run' if dry_run else 'live'})...\n")

    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gh_client = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"])) if not dry_run else None

    results = {"success": [], "skipped": [], "failed": []}

    for entry in stale_entries:
        key = f"{entry['pkg']}::{entry['fn']}"
        print(f"→ {key} ({entry['status']})")

        remediation = remediate_entry(anthropic_client, entry)
        if remediation is None:
            print(f"  ✗ Agent failed — skipping\n")
            results["failed"].append(key)
            continue

        action = remediation.get("action", "no_change")
        print(f"  Action:    {action}")
        print(f"  Rationale: {remediation.get('rationale', '')}")

        if action == "no_change":
            print(f"  → No change needed.\n")
            results["skipped"].append(key)
            continue

        if dry_run:
            print(f"  → Dry run: would open PR.\n")
            results["success"].append(key)
            continue

        corrected = remediation.get("corrected_entry")
        if not corrected:
            print(f"  ✗ No corrected entry for action '{action}' — skipping\n")
            results["failed"].append(key)
            continue

        pkg      = corrected["pkg"]
        fn       = corrected["fn"]
        new_from = corrected["from_version"]

        new_path     = f"entries/{pkg}/{pkg}__{fn}__{version_to_dashes(new_from)}.json"
        old_path_obj = old_filepath(entries_dir, pkg, fn, entry["from_version"])
        old_path     = (f"entries/{pkg}/{old_path_obj.name}"
                        if old_path_obj else None)

        files_to_delete = (
            [old_path]
            if old_path and old_path != new_path and action == "raise_floor"
            else []
        )

        safe_key = f"{pkg}-{fn}".replace("::", "-").replace(".", "-")
        branch   = f"fix/db-staleness-{safe_key}-{datetime.date.today().isoformat()}"

        action_prefixes = {
            "raise_floor":    "fix(db): raise from_version",
            "extend_ceiling": "fix(db): extend to_version",
            "close":          "fix(db): close entry",
        }
        pr_title = (
            f"{action_prefixes.get(action, 'fix(db):')} "
            f"{pkg}::{fn} [{entry['status']}]"
        )
        pr_body = build_pr_body(entry, remediation)

        if action == "close":
            corrected["closed"] = True

        try:
            pr_url = create_pr(
                gh=gh_client,
                repo_name=args.repo,
                branch=branch,
                base="main",
                title=pr_title,
                body=pr_body,
                files=[(new_path, json.dumps(corrected, indent=2))],
                files_to_delete=files_to_delete,
            )
            print(f"  ✓ PR opened: {pr_url}\n")
            results["success"].append(key)
        except Exception as e:
            print(f"  ✗ Failed to create PR: {e}\n")
            results["failed"].append(key)

    print("─" * 60)
    print(f"Complete: {len(results['success'])} PRs opened, "
          f"{len(results['skipped'])} skipped (no change), "
          f"{len(results['failed'])} failed")

    if results["failed"]:
        print(f"Failed entries: {', '.join(results['failed'])}")
        sys.exit(1)


if __name__ == "__main__":
    main()