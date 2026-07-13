#!/usr/bin/env python3
"""
reproducr-db staleness remediation agent.

For each stale database entry, calls Claude to:
  1. Fetch the official changelog (pre-fetched once per package, not per entry)
  2. Determine the correct action (raise_floor / extend_ceiling / close / no_change)
  3. Draft the corrected JSON entry
  4. Open a PR in reproducr-db for human review

Cost optimisation: changelogs are fetched once per unique package via HTTP
before the agent loop, then passed directly in the user message. web_search
is disabled for entries whose changelog was successfully pre-fetched, cutting
per-run cost by ~70-80%.

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
import urllib.request
import urllib.error
from pathlib import Path

import anthropic
from github import Github, Auth, GithubException


# ── Constants ────────────────────────────────────────────────────────────────

MODEL      = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048

# Maximum changelog characters to pass to Claude per package.
# Recent releases appear at the top of most changelogs so the first
# 25 000 chars covers several years of history.
CHANGELOG_MAX_CHARS = 25_000

# Known changelog URLs — pre-fetched once before the entry loop.
# Entries for packages not listed here fall back to web_search.
CHANGELOG_URLS: dict[str, str] = {
    "dplyr":      "https://dplyr.tidyverse.org/news/index.html",
    "ggplot2":    "https://ggplot2.tidyverse.org/news/index.html",
    "tidyr":      "https://tidyr.tidyverse.org/news/index.html",
    "purrr":      "https://purrr.tidyverse.org/news/index.html",
    "tibble":     "https://tibble.tidyverse.org/news/index.html",
    "stringr":    "https://stringr.tidyverse.org/news/index.html",
    "readr":      "https://readr.tidyverse.org/news/index.html",
    "lubridate":  "https://lubridate.tidyverse.org/news/index.html",
    "forcats":    "https://forcats.tidyverse.org/news/index.html",
    "data.table": "https://raw.githubusercontent.com/Rdatatable/data.table/master/NEWS.md",
    "survival":   "https://raw.githubusercontent.com/therneau/survival/master/NEWS.md",
    "lme4":       "https://cran.r-project.org/web/packages/lme4/news/news.html",
    "MatchIt":    "https://kosukeimai.github.io/MatchIt/news/index.html",
    "broom":      "https://broom.tidymodels.org/news/index.html",
    "cobalt":     "https://ngreifer.github.io/cobalt/news/index.html",
    "caret":      "https://cran.r-project.org/web/packages/caret/news/news.html",
    "rstan":      "https://mc-stan.org/rstan/",
}

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

## Changelog research
If a pre-fetched changelog is provided in the user message, use it directly
and do NOT call web_search for this package — the changelog is already there.
Only call web_search if no changelog is provided, or if the pre-fetched
content is clearly insufficient for the specific version range in question.

## Mandatory analysis steps
Before recommending ANY action you must:

1. Read the official changelog (pre-fetched or searched).
   Identify the EXACT version the breaking change was introduced.
   Note: this may be a mid-minor release (e.g. 1.0.8, not 1.0.0).
   Cite the specific changelog section, version number, and date.

2. Identify the EXACT last version where the old (safe) behaviour existed.

3. Determine who is ACTUALLY at risk today:
   - What is the current CRAN version?
   - What versions could a real active project plausibly have installed?
   - Is the breaking-change window still reachable by any realistic project?
   - Has enough time passed that virtually all users have transitioned?
   - Are there long-term support environments (pharma, clinical) where
     pinned old versions are realistic?

4. Check whether intermediate minor series existed on CRAN:
   - If a package jumped from 0.8.x directly to 1.0.0 with no 0.9.x,
     raising the sentinel to 0.9.99 is cosmetic — state this explicitly.

## Decision framework — work through these questions in order

### Q1: Is the entire version window archaeologically unreachable?
The window (from_version, to_version] is unreachable if no realistic active
project could have a version within it. Indicators:
- The transition happened 5+ years ago AND current CRAN is 2+ major versions
  ahead of to_version
- No pinned environment (including regulated/pharma) would plausibly still
  be within the window
- The function did not exist at from_version (e.g. pivot_wider in tidyr 0.8.x)

If YES → action: close

### Q2: For stale_floor — who is actually still at risk?
The stale_floor flag fires when from_version is >= 1 major version behind
current CRAN. This does NOT automatically mean raise_floor. Work through:

a) What exactly is the breaking change and which version introduced it?

b) If the breaking change landed mid-minor (e.g. 1.0.8 not 1.0.0):
   - A from_version of "0.8.99" flags everyone above 0.8.99, which is
     correct IF users on 1.0.8-to_version still exist in practice.

c) Are users on intermediate versions still realistic in pharma/pinned
   environments?

d) Is the window still producing true positives for any realistic user?
   If NO → close (not raise_floor)
   If YES but floor is too wide → raise_floor

### Q3: For stale_ceiling — does the breaking change still apply?
a) Read changelogs for versions between to_version and current CRAN
b) Was the breaking change reverted, fixed, or superseded?
c) Does the risk still apply in the current release?
If YES → extend_ceiling (X.Y.9 sentinel)
If NO → update description, narrow to_version, or close

### Q4: Sentinel correctness for raise_floor
The sentinel pattern is X.Y.99 — always the last patch of a minor series.
NEVER set from_version to an actual released version number (e.g. "0.8.5").
NEVER lower from_version (which would widen the window).
Only raise from_version (narrowing the window from below).
Verify the intermediate minor series actually existed on CRAN before raising.

## Actions

- raise_floor: raise from_version sentinel to narrow the window from below.
  NEVER lower the sentinel. NEVER use a real version number.
  Example: breaking change in 1.0.0, 0.9.x existed → from_version = "0.9.99"
  Example: breaking change in 1.0.0, no 0.9.x → no_change (cosmetic only)

- extend_ceiling: raise to_version to cover the current release series.
  Always use X.Y.9 sentinel: "1.2.9", "4.0.9" etc.

- close: permanently suppress this entry.
  Use ONLY when the entire window is archaeologically unreachable.
  Do NOT close if any realistic user population could still be at risk.

- no_change: the staleness flag is a false positive on inspection.
  Always state explicitly why no change is needed and why the flag fired.

## Regulated environment requirements
- False positive: wastes analyst time, erodes trust, may delay submissions
- False negative: could compromise reproducibility of regulated analyses

Therefore:
- Never guess. If uncertain, use no_change and state what is missing.
- Cite the specific changelog section, version number, and date for every
  factual claim.
- Do not infer from package behaviour — only cite documented changes.

## Response format — CRITICAL
Your FINAL response must be a JSON object and nothing else.
No prose before or after. No markdown fences. No explanation outside JSON.
Just the raw JSON object:
{
  "action": "raise_floor|extend_ceiling|close|no_change",
  "rationale": "2-3 sentences: (1) cite exact changelog evidence with version
                and date, (2) state who is realistically at risk today,
                (3) explain why this action is correct.",
  "corrected_entry": { ...full corrected entry with all original fields... }
}
For close or no_change, set corrected_entry to null.
"""


# ── Changelog pre-fetcher ────────────────────────────────────────────────────

def fetch_changelog(pkg: str, url: str, timeout: int = 20) -> str | None:
    """
    Fetch a package changelog via HTTP. Returns the text content (truncated
    to CHANGELOG_MAX_CHARS) or None on failure.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "reproducr-db-agent/1.0 (github.com/repro-stats)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        if len(content) > CHANGELOG_MAX_CHARS:
            content = content[:CHANGELOG_MAX_CHARS] + "\n\n[... changelog truncated ...]"
        return content
    except Exception as e:
        print(f"  ⚠  Could not pre-fetch changelog for {pkg}: {e}")
        return None


def prefetch_changelogs(pkgs: set[str]) -> dict[str, str]:
    """
    Fetch changelogs for all known packages in pkgs.
    Returns a dict of {pkg: changelog_text}.
    """
    changelogs: dict[str, str] = {}
    known = {p for p in pkgs if p in CHANGELOG_URLS}
    unknown = pkgs - known

    if known:
        print(f"Pre-fetching changelogs for: {', '.join(sorted(known))}")
        for pkg in sorted(known):
            text = fetch_changelog(pkg, CHANGELOG_URLS[pkg])
            if text:
                changelogs[pkg] = text
                print(f"  ✓  {pkg} ({len(text):,} chars)")
            else:
                print(f"  ✗  {pkg} — will fall back to web_search")

    if unknown:
        print(f"No pre-fetch URL for: {', '.join(sorted(unknown))} — will use web_search")

    return changelogs


# ── JSON parsing helper ───────────────────────────────────────────────────────

def _parse_json(text: str) -> dict | None:
    """Extract and parse the first JSON object found in text."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


# ── Claude call ──────────────────────────────────────────────────────────────

def remediate_entry(
    client: anthropic.Anthropic,
    entry: dict,
    changelog: str | None = None,
) -> dict | None:
    """
    Ask Claude to determine the remediation for one stale entry.
    If changelog is provided, it is passed directly and web_search is disabled.
    Returns the parsed remediation dict, or None on failure.
    """
    today = datetime.date.today().isoformat()

    if changelog:
        changelog_block = (
            f"\nOFFICIAL CHANGELOG (pre-fetched — read this carefully, "
            f"do NOT call web_search):\n"
            f"<changelog pkg=\"{entry['pkg']}\">\n{changelog}\n</changelog>\n"
        )
        tools        = []   # web_search not needed — changelog already provided
        research_instruction = (
            "Use the pre-fetched changelog above. "
            "Do NOT call web_search — the changelog is already provided."
        )
    else:
        changelog_block      = ""
        tools                = [{"type": "web_search_20250305", "name": "web_search"}]
        research_instruction = (
            f"Use web_search to fetch the changelog at: "
            f"{entry.get('reference', 'search for ' + entry['pkg'] + ' changelog')}"
        )

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
{changelog_block}
Required steps:
1. {research_instruction}
2. Identify the EXACT version the breaking change was introduced and the
   EXACT last safe version. Cite changelog section, version number, date.
3. Determine who is realistically at risk today, considering pharma/clinical
   contexts with renv-pinned package versions.
4. Work through the decision framework (Q1 → Q4) before deciding.
5. Output your final answer as a raw JSON object only —
   no prose before or after, no markdown fences.
"""

    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=tools,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw         = "\n".join(text_blocks).strip()
    result      = _parse_json(raw)

    if result is None:
        print("  ↩  No JSON in initial response — requesting JSON-only follow-up")
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
        response2   = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text_blocks2 = [b.text for b in response2.content if b.type == "text"]
        raw2         = "\n".join(text_blocks2).strip()
        result       = _parse_json(raw2)

        if result is None:
            print(f"  ✗  Could not parse Claude response after follow-up for "
                  f"{entry['pkg']}::{entry['fn']}")
            print(f"  Raw: {raw2[:400]}")

    return result


# ── File path helpers ─────────────────────────────────────────────────────────

def version_to_dashes(version: str) -> str:
    return version.replace(".", "-")


def entry_filepath(entries_dir: Path, pkg: str, fn: str, from_version: str) -> Path:
    return entries_dir / pkg / f"{pkg}__{fn}__{version_to_dashes(from_version)}.json"


def old_filepath(entries_dir: Path, pkg: str, fn: str, from_version: str) -> Path | None:
    """Find existing file for this entry (from_version may differ)."""
    pkg_dir = entries_dir / pkg
    if not pkg_dir.exists():
        return None
    matches = list(pkg_dir.glob(f"{pkg}__{fn}__*.json"))
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
    repo     = gh.get_repo(repo_name)
    base_sha = repo.get_branch(base).commit.sha

    # Create or reuse branch
    try:
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
    except GithubException as e:
        if e.status == 422:
            print(f"  Branch {branch} already exists, reusing.")
        else:
            raise

    # Commit files
    for path, content in files:
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.update_file(
                path=path, message=f"chore: update {path}",
                content=content, sha=existing.sha, branch=branch,
            )
        except GithubException:
            repo.create_file(
                path=path, message=f"chore: add {path}",
                content=content, branch=branch,
            )

    # Delete old files (e.g. renamed from_version)
    for path in (files_to_delete or []):
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.delete_file(
                path=path, message=f"chore: remove old entry {path}",
                sha=existing.sha, branch=branch,
            )
        except GithubException:
            pass

    # Check for existing open PR on this branch before creating a new one
    owner_login  = repo.owner.login
    existing_prs = list(repo.get_pulls(state="open", head=f"{owner_login}:{branch}"))
    if existing_prs:
        pr_url = existing_prs[0].html_url
        print(f"  → PR already open: {pr_url}")
        return pr_url

    pr = repo.create_pull(title=title, body=body, head=branch, base=base)
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


# ── Schema constants ──────────────────────────────────────────────────────────

# Fields that belong in the JSON schema — everything else is stripped
# before writing to disk to prevent validate CI failures.
_SCHEMA_FIELDS = frozenset({
    "pkg", "fn", "from_version", "to_version", "risk",
    "description", "reference", "added_by", "added_date", "closed",
})

# Fields present in stale_entries.json (from check_db_staleness) that must
# not be forwarded to Claude or included in corrected_entry.
_STALENESS_ONLY = frozenset({"key", "status", "gap", "current_version"})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="reproducr-db staleness remediation agent"
    )
    parser.add_argument("--stale-file",    required=True)
    parser.add_argument("--entries-dir",   required=True)
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

    # ── Pre-fetch changelogs (one HTTP request per package, not per entry) ──
    unique_pkgs = {e["pkg"] for e in stale_entries}
    changelogs  = prefetch_changelogs(unique_pkgs)
    print()

    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gh_client = (
        Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
        if not dry_run else None
    )

    results = {"success": [], "skipped": [], "failed": []}

    for raw_entry in stale_entries:
        # ── Enrich entry from disk ──────────────────────────────────────────
        # check_db_staleness() omits description, reference, risk etc.
        # Read the full JSON from disk and overlay the staleness fields.
        existing_path = old_filepath(
            entries_dir, raw_entry["pkg"], raw_entry["fn"], raw_entry["from_version"]
        )
        if existing_path and existing_path.exists():
            with open(existing_path) as f:
                entry = json.load(f)
            entry["status"]          = raw_entry["status"]
            entry["gap"]             = raw_entry.get("gap")
            entry["current_version"] = raw_entry.get("current_version")
        else:
            entry = raw_entry.copy()

        # Strip staleness-only fields before sending to Claude so they are
        # never reflected back in corrected_entry.
        entry = {k: v for k, v in entry.items() if k not in _STALENESS_ONLY}

        key = f"{entry['pkg']}::{entry['fn']}"
        print(f"→ {key} ({entry['status']})")

        # ── Call Claude ─────────────────────────────────────────────────────
        remediation = remediate_entry(
            anthropic_client,
            entry,
            changelog=changelogs.get(entry["pkg"]),
        )
        if remediation is None:
            print(f"  ✗  Agent failed — skipping\n")
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

        # ── Resolve corrected entry ─────────────────────────────────────────
        corrected = remediation.get("corrected_entry")

        if action == "close" and not corrected:
            # corrected_entry is legitimately null for close — derive from disk
            if existing_path and existing_path.exists():
                with open(existing_path) as f:
                    corrected = json.load(f)
                print(f"  ↩  close: derived from {existing_path.name}")
            else:
                # Fallback: use the enriched entry as base
                corrected = {k: v for k, v in entry.items()
                             if k not in _STALENESS_ONLY}
                print("  ↩  close: no disk file found; using stale entry as base")

        if not corrected:
            print(f"  ✗  No corrected entry for action '{action}' — skipping\n")
            results["failed"].append(key)
            continue

        # Guard: required fields must all be present
        for field in ("pkg", "fn", "from_version", "to_version",
                      "risk", "description", "reference"):
            if field not in corrected:
                print(f"  ✗  corrected_entry missing '{field}' — skipping\n")
                results["failed"].append(key)
                corrected = None
                break
        if corrected is None:
            continue

        # ── Determine file paths ────────────────────────────────────────────
        pkg      = corrected["pkg"]
        fn       = corrected["fn"]
        new_from = (
            raw_entry["from_version"] if action == "close"
            else corrected["from_version"]
        )

        new_path     = f"entries/{pkg}/{pkg}__{fn}__{version_to_dashes(new_from)}.json"
        old_path_obj = old_filepath(entries_dir, pkg, fn, raw_entry["from_version"])
        old_path     = (f"entries/{pkg}/{old_path_obj.name}"
                        if old_path_obj else None)

        files_to_delete = (
            [old_path]
            if old_path and old_path != new_path and action == "raise_floor"
            else []
        )

        # ── Duplicate PR check ──────────────────────────────────────────────
        safe_key = f"{pkg}-{fn}".replace("::", "-").replace(".", "-")
        branch   = f"fix/db-staleness-{safe_key}"

        repo      = gh_client.get_repo(args.repo)
        open_prs  = list(repo.get_pulls(state="open"))
        existing  = [pr for pr in open_prs if f"{pkg}::{fn}" in pr.title]
        if existing:
            print(f"  → Open PR already exists: {existing[0].html_url} — skipping\n")
            results["skipped"].append(key)
            continue

        # ── Build PR content ────────────────────────────────────────────────
        action_prefixes = {
            "raise_floor":    "fix(db): raise from_version",
            "extend_ceiling": "fix(db): extend to_version",
            "close":          "fix(db): close entry",
        }
        pr_title = (
            f"{action_prefixes.get(action, 'fix(db):')} "
            f"{pkg}::{fn} [{raw_entry['status']}]"
        )
        pr_body = build_pr_body(raw_entry, remediation)

        if action == "close":
            corrected["closed"] = True

        # Strip any non-schema fields before writing JSON to disk
        corrected = {k: v for k, v in corrected.items() if k in _SCHEMA_FIELDS}

        # ── Open PR ─────────────────────────────────────────────────────────
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
            print(f"  ✓  PR: {pr_url}\n")
            results["success"].append(key)
        except Exception as e:
            print(f"  ✗  Failed to create PR: {e}\n")
            results["failed"].append(key)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("─" * 60)
    print(f"Complete: {len(results['success'])} PRs opened, "
          f"{len(results['skipped'])} skipped (no change), "
          f"{len(results['failed'])} failed")

    try:
        with open("remediation_report.json", "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print("Wrote remediation_report.json")
    except Exception as e:
        print(f"Could not write remediation_report.json: {e}")

    if results["failed"]:
        print(f"Failed entries: {', '.join(results['failed'])}")

    if results["failed"] and fail_on_error:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()