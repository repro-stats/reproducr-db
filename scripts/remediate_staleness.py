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
You are a senior statistical software engineer maintaining the reproducr-db
breaking-changes database for the R package reproducr. This database is used
in pharmaceutical, clinical trial, and regulated research environments where
false positives and false negatives both have real consequences. Precision
and accuracy are non-negotiable.

## Your role
You review stale database entries and determine the correct remediation.
Every decision you make will affect whether regulated R users are correctly
warned about breaking changes — or incorrectly flagged for changes they are
not exposed to. Think carefully before recommending any action.

## Database schema
Each entry defines a half-open version window (from_version, to_version]:
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

A user is flagged if and only if their installed version V satisfies:
  from_version < V <= to_version

## Staleness types
- stale_ceiling: current CRAN version is above to_version. The window ceiling
  may need extending if the breaking change still applies in newer versions.
- stale_floor: from_version is >= 1 major version behind current CRAN. The
  window floor may be too wide, flagging users who upgraded past the risk
  long ago.

## Mandatory research steps
Before recommending ANY action you must:

1. Fetch the official changelog using web_search. Never rely on memory.
   Always retrieve the actual changelog text and read it directly.
   Common changelog URLs:
   - tidyverse packages: https://{pkg}.tidyverse.org/news/index.html
   - CRAN packages: https://cran.r-project.org/web/packages/{pkg}/news/news.html
   - GitHub: https://github.com/{owner}/{pkg}/blob/main/NEWS.md

2. Identify the EXACT version the breaking change was introduced.
   Note: this may be a mid-minor release (e.g. 1.0.8, not 1.0.0).
   Cite the specific changelog section, version number, and date.

3. Identify the EXACT last version where the old (safe) behaviour existed.

4. Determine who is ACTUALLY at risk today:
   - What is the current CRAN version?
   - What versions could a real active project plausibly have installed?
   - Is the breaking-change window still reachable by any realistic project?
   - Has enough time passed that virtually all users have transitioned?
   - Are there long-term support environments (pharma, clinical) where
     pinned old versions are realistic?

5. Check whether intermediate minor series existed on CRAN:
   - If a package jumped from 0.8.x directly to 1.0.0 with no 0.9.x,
     raising the sentinel to 0.9.99 is cosmetic — state this explicitly.
   - Verify on CRAN archive that the intermediate series actually existed.

## Decision framework — work through these questions in order

### Q1: Is the entire version window archaeologically unreachable?
The window (from_version, to_version] is unreachable if no realistic active
project could have a version within it. Indicators:
- to_version covers only R 3.x or pre-2019 releases
- The package has gone through multiple major rewrites since to_version
- The transition happened 5+ years ago AND current CRAN is 2+ major versions
  ahead of to_version
- No pinned environment (including regulated/pharma) would plausibly still
  be within the window

If YES → action: close
State explicitly: why the window is unreachable, what to_version is, what
current CRAN is, and why no realistic user falls within the window.

### Q2: For stale_floor — who is actually still at risk?
The stale_floor flag fires when from_version is >= 1 major version behind
current CRAN. This does NOT automatically mean raise_floor. Work through:

a) What exactly is the breaking change and which version introduced it?

b) If the breaking change landed mid-minor (e.g. 1.0.8 not 1.0.0):
   - Users on versions BELOW 1.0.8 have the old (safe) behaviour
   - Users on 1.0.8 up to to_version have the new (breaking) behaviour
   - Users ABOVE to_version are already past the transition
   - A from_version of "0.8.99" flags everyone above 0.8.99 — this
     includes users on 1.0.8 through to_version, which is correct IF
     those users still exist in practice

c) Are users on intermediate versions still realistic?
   - In a clinical/pharma context with renv-pinned environments, yes
   - In general R usage, users on 1.0.8 (released 2020) are unlikely
     if current CRAN is 1.2.1 (released 2023+)

d) Is the window still producing true positives for any realistic user?
   If NO → close (not raise_floor)
   If YES but floor is capturing users who upgraded long ago → raise_floor

### Q3: For stale_ceiling — does the breaking change still apply?
a) Fetch changelogs for all versions between to_version and current CRAN
b) Was the breaking change reverted, fixed, or superseded?
c) Does the risk still apply in the current release?
If YES → extend_ceiling to cover the current release series (X.Y.9 sentinel)
If NO → update description, narrow to_version, or close

### Q4: Sentinel correctness for raise_floor
The sentinel pattern is X.Y.99 — always the last patch of a minor series.
NEVER set from_version to an actual released version number (e.g. "0.8.5").
NEVER lower from_version (which would widen the window).
Only raise from_version (narrowing the window from below).

Before raising, verify:
- The intermediate minor series actually existed on CRAN
- Raising the sentinel excludes ONLY users who are no longer at realistic risk
- The new from_version does not accidentally exclude users who ARE still at risk

If the breaking change landed mid-minor (e.g. 1.0.8):
- from_version = "1.0.99" excludes everyone on 1.0.x from being flagged
- Only do this if users on 1.0.8-1.0.x are no longer a realistic concern
- If 1.0.x pinned environments are plausible (especially in regulated contexts)
  keep from_version at "0.8.99" or "0.9.99" depending on whether 0.9.x existed

## Actions

- raise_floor: raise from_version sentinel to narrow the window from below.
  Use ONLY when the new sentinel excludes users who are genuinely no longer
  at risk, while still capturing all users who are.
  NEVER lower the sentinel. NEVER use a real version number.
  Example: breaking change in 1.0.0, 0.9.x existed → from_version = "0.9.99"
  Example: breaking change in 1.0.0, no 0.9.x → no_change (cosmetic only)

- extend_ceiling: raise to_version to cover the current release series.
  Use when the breaking change still applies in versions above to_version.
  Always use X.Y.9 sentinel: "1.2.9", "4.0.9" etc.

- close: permanently suppress this entry.
  Use ONLY when the entire window (from_version, to_version] is
  archaeologically unreachable — no realistic active project, including
  regulated/pinned environments, could be on a version within it.
  This is often the right action for stale_floor entries where:
  - The transition happened years ago
  - Current CRAN is multiple major versions ahead
  - Even conservative pharma environments would have updated by now
  Do NOT close if any realistic user population could still be at risk.

- no_change: the staleness flag is a false positive on inspection.
  Use when the entry is correctly defined despite the staleness flag.
  Always state explicitly why no change is needed and why the flag fired.

## Regulated environment requirements
This database is used in pharmaceutical and clinical research. Errors have
real consequences:
- False positive (flagging safe code): wastes analyst time, erodes trust
  in the tool, may delay regulated submissions
- False negative (missing real risk): could compromise reproducibility of
  regulated analyses, clinical trial results, or regulatory submissions

Therefore:
- Never guess. If you cannot determine the correct action with certainty
  from the changelog, use no_change and state what information is missing.
- Cite the specific changelog section, version number, and date for every
  factual claim.
- If the changelog is ambiguous, say so explicitly in the rationale.
- Do not infer from package behaviour — only cite documented changes.
- When in doubt between raise_floor and close, prefer close for old entries
  where the transition clearly predates most active projects.

## Response format — CRITICAL
Your FINAL response must be a JSON object and nothing else.
No prose before or after. No markdown fences. No explanation outside JSON.
Just the raw JSON object:
{
  "action": "raise_floor|extend_ceiling|close|no_change",
  "rationale": "2-3 sentences: (1) cite the exact changelog evidence with
                version and date, (2) state who is realistically at risk
                today and why, (3) explain why this action is correct.",
  "corrected_entry": { ...full corrected entry with all original fields... }
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
This database is used in regulated pharmaceutical and clinical research
environments. Precision is critical — work through the decision framework
carefully before choosing an action.

Entry JSON:
{json.dumps(entry, indent=2)}

Staleness status: {entry['status']}
Gap description:  {entry.get('gap', 'N/A')}
Today's date:     {today}

Required steps — do not skip any:
1. Use web_search to fetch the official changelog at:
   {entry.get('reference', 'search for ' + entry['pkg'] + ' changelog')}
2. Identify the EXACT version the breaking change was introduced and the
   EXACT last safe version.
3. Determine who is realistically at risk today, considering that this
   database is used in pharma/clinical contexts with potentially pinned
   old package versions.
4. Work through the decision framework (Q1 through Q4) before deciding.
5. Output your final answer as a raw JSON object only —
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
                '"rationale": "2-3 sentences citing exact changelog evidence, '
                'who is at risk today, and why this action is correct", '
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

    # Check for an existing open PR on this branch before creating a new one.
    # A branch reused from a previous run will already have a PR — return its
    # URL rather than failing with a 422 duplicate error.
    owner_login = repo.owner.login
    existing_prs = list(repo.get_pulls(
        state="open",
        head=f"{owner_login}:{branch}",
    ))
    if existing_prs:
        pr_url = existing_prs[0].html_url
        print(f"  → PR already open: {pr_url}")
        return pr_url

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
    parser.add_argument("--dry-run",       default="false")
    parser.add_argument("--repo",          required=True)
    parser.add_argument("--fail-on-error", default="false",
                        help="Exit non-zero when any entry failed (default: false)")
    args = parser.parse_args()

    dry_run       = args.dry_run.lower() == "true"
    fail_on_error = args.fail_on_error.lower() == "true"
    entries_dir   = Path(args.entries_dir)

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
        # Enrich the stale entry with the full JSON from disk before sending
        # to Claude. check_db_staleness() only returns schema columns (key,
        # pkg, fn, from_version, to_version, current_version, status, gap)
        # and omits description, reference, risk, added_by, added_date.
        # Without description and reference Claude cannot research the entry
        # correctly and defaults to no_change due to missing documentation.
        existing_path = old_filepath(entries_dir, entry["pkg"], entry["fn"],
                                     entry["from_version"])
        if existing_path and existing_path.exists():
            with open(existing_path) as f:
                full_entry = json.load(f)
            # Overlay staleness fields — these are not in the disk file
            full_entry["status"]          = entry["status"]
            full_entry["gap"]             = entry.get("gap")
            full_entry["current_version"] = entry.get("current_version")
            entry = full_entry

        # Strip staleness-only fields that are not part of the JSON schema.
        # If the disk file was not found, the raw stale entry (which includes
        # 'key') is used as-is — remove non-schema fields before sending to
        # Claude so they are never included in the corrected_entry.
        _NON_SCHEMA = ("key",)
        entry = {k: v for k, v in entry.items() if k not in _NON_SCHEMA}

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

        # For close, corrected_entry is legitimately null per the JSON contract.
        # Derive the corrected object from the existing entry file on disk if
        # present; otherwise fall back to the incoming stale entry so we can
        # still record the closure. This prevents failures when the entry file
        # is not present on disk (e.g. newly added entries not yet on main).
        if action == "close" and not corrected:
            existing_path = old_filepath(entries_dir, entry["pkg"], entry["fn"],
                                         entry["from_version"])
            if existing_path and existing_path.exists():
                with open(existing_path) as f:
                    corrected = json.load(f)
                print(f"  ↩ close: derived corrected entry from {existing_path.name}")
            else:
                corrected = entry.copy()
                print("  ↩ close: no existing entry file found; "
                      "using stale entry as base for close action.")

        if not corrected:
            print(f"  ✗ No corrected entry for action '{action}' — skipping\n")
            results["failed"].append(key)
            continue

        # Guard: ensure required fields are present (agent occasionally drops them)
        for required_field in ("pkg", "fn", "from_version", "to_version",
                               "risk", "description", "reference"):
            if required_field not in corrected:
                print(f"  ✗ corrected_entry missing required field "
                      f"'{required_field}' — skipping\n")
                results["failed"].append(key)
                corrected = None
                break
        if corrected is None:
            continue

        pkg      = corrected["pkg"]
        fn       = corrected["fn"]
        # For close, always keep the existing file path (from_version unchanged).
        # For raise_floor/extend_ceiling, use the corrected from_version.
        new_from = entry["from_version"] if action == "close" else corrected["from_version"]

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

        # Strip any staleness-only or agent-added fields that are not part
        # of the reproducr-db JSON schema before writing the file.
        # Leaving extra fields (e.g. 'key', 'status', 'gap', 'current_version')
        # causes the validate CI check to fail with "Additional properties
        # are not allowed".
        _SCHEMA_FIELDS = {
            "pkg", "fn", "from_version", "to_version", "risk",
            "description", "reference", "added_by", "added_date", "closed",
        }
        corrected = {k: v for k, v in corrected.items() if k in _SCHEMA_FIELDS}

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
            print(f"  ✓ PR: {pr_url}\n")
            results["success"].append(key)
        except Exception as e:
            print(f"  ✗ Failed to create PR: {e}\n")
            results["failed"].append(key)

    print("─" * 60)
    print(f"Complete: {len(results['success'])} PRs opened, "
          f"{len(results['skipped'])} skipped (no change), "
          f"{len(results['failed'])} failed")

    # Write machine-readable report for workflow artifact upload
    try:
        with open("remediation_report.json", "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print("Wrote remediation_report.json")
    except Exception as e:
        print(f"Could not write remediation_report.json: {e}")

    if results["failed"]:
        print(f"Failed entries: {', '.join(results['failed'])}")

    # Exit non-zero only when explicitly requested — prevents transient or
    # partial failures from failing the whole workflow run by default.
    if results["failed"] and fail_on_error:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()