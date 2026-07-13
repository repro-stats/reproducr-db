#!/usr/bin/env python3
"""
reproducr-db staleness remediation agent.

For each stale database entry, calls Claude to:
  1. Fetch the official changelog (pre-fetched once per package, stripped of HTML)
  2. Determine the correct action (raise_floor / extend_ceiling / close / no_change)
  3. Draft the corrected JSON entry
  4. Open a PR in reproducr-db for human review

Cost optimisation:
  - Changelogs fetched once per package via HTTP (not web_search per entry)
  - HTML stripped to plain text before passing to Claude (3-5x token reduction)
  - Entries batched by package — one API call per package, not per entry
  - web_search disabled when changelog is pre-fetched

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
from collections import defaultdict
from pathlib import Path

import anthropic
from github import Github, Auth, GithubException


# ── Constants ────────────────────────────────────────────────────────────────

MODEL      = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048

# Plain-text chars to pass per package changelog.
# After HTML stripping, 12k chars covers 3-4 years of active package history.
CHANGELOG_MAX_CHARS = 12_000

# Known changelog URLs — pre-fetched once before the entry loop.
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
A user is flagged if and only if: from_version < installed_version <= to_version

## Staleness types
- stale_ceiling: current CRAN above to_version — may need extending
- stale_floor: from_version >= 1 major version behind CRAN — may be too wide

## Changelog research
If a pre-fetched changelog is provided, use it directly — do NOT web_search.
Only call web_search if no changelog is provided or content is clearly
insufficient for the specific version range.

## Decision framework (apply per entry)

### Q1: Is the window archaeologically unreachable?
Indicators: transition 5+ years ago AND CRAN is 2+ major versions ahead
of to_version, OR function did not exist at from_version.
YES → close

### Q2: stale_floor — who is actually still at risk?
a) What exactly changed and in which version?
b) Mid-minor changes (e.g. 1.0.8): from_version "0.8.99" still flags
   1.0.8–to_version correctly if those users exist in practice.
c) Pharma/clinical renv-pinned environments: versions up to 3-4 years old
   are realistic.
d) Window still producing true positives? NO → close. YES but too wide → raise_floor.

### Q3: stale_ceiling — does the change still apply?
Reverted/fixed/superseded? NO → close or narrow. Still applies? YES → extend_ceiling.

### Q4: Sentinel correctness for raise_floor
X.Y.99 pattern only. NEVER use a real version number. NEVER lower from_version.
Verify the intermediate minor series existed on CRAN before raising.

## Actions
- raise_floor: raise from_version sentinel (narrowing window from below)
- extend_ceiling: raise to_version to X.Y.9 to cover current release series
- close: entire window archaeologically unreachable — no realistic user at risk
- no_change: staleness flag is a false positive — entry is correct as-is

## Regulated environment requirements
- False positive: wastes analyst time, may delay regulated submissions
- False negative: could compromise reproducibility of regulated analyses
Never guess. Cite specific changelog version, section, and date for every claim.

## Response format
Return a JSON object with a "remediations" array — one item per entry, in
the same order as provided. No prose, no markdown fences.
{
  "remediations": [
    {
      "fn": "filter",
      "action": "raise_floor|extend_ceiling|close|no_change",
      "rationale": "2-3 sentences: changelog evidence (version+date), who is at risk, why this action.",
      "corrected_entry": { ...full entry with all original fields... } or null
    }
  ]
}
corrected_entry must be null for close and no_change.
"""


# ── HTML stripping ────────────────────────────────────────────────────────────

def strip_html(html: str) -> str:
    """
    Strip HTML tags and decode common entities to produce clean plain text.
    Reduces token count by 3-5x compared to raw HTML.
    """
    # Remove script and style blocks entirely
    html = re.sub(
        r'<(script|style)[^>]*>.*?</\1>', '', html,
        flags=re.DOTALL | re.IGNORECASE
    )
    # Replace block-level tags with newlines for readability
    html = re.sub(
        r'<(br|p|div|li|h[1-6]|section|article)[^>]*/?>',
        '\n', html, flags=re.IGNORECASE
    )
    # Remove all remaining tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode common HTML entities
    for entity, char in [
        ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
        ('&nbsp;', ' '), ('&#39;', "'"), ('&quot;', '"'),
        ('&ldquo;', '"'), ('&rdquo;', '"'), ('&mdash;', '—'),
        ('&ndash;', '–'), ('&lsquo;', "'"), ('&rsquo;', "'"),
    ]:
        html = html.replace(entity, char)
    # Collapse whitespace while preserving paragraph breaks
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


# ── Changelog pre-fetcher ────────────────────────────────────────────────────

def fetch_changelog(pkg: str, url: str, timeout: int = 20) -> str | None:
    """
    Fetch a package changelog, strip HTML, and truncate.
    Returns plain-text content or None on failure.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "reproducr-db-agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        # Strip HTML if content looks like HTML
        if "<html" in raw[:200].lower() or "<!doctype" in raw[:200].lower():
            text = strip_html(raw)
        else:
            text = raw  # Already plain text (e.g. raw NEWS.md)
        if len(text) > CHANGELOG_MAX_CHARS:
            text = text[:CHANGELOG_MAX_CHARS] + "\n\n[... changelog truncated ...]"
        return text
    except Exception as e:
        print(f"  ⚠  Could not pre-fetch changelog for {pkg}: {e}")
        return None


def prefetch_changelogs(pkgs: set[str]) -> dict[str, str]:
    """Fetch changelogs for all known packages. Returns {pkg: plain_text}."""
    changelogs: dict[str, str] = {}
    known   = {p for p in pkgs if p in CHANGELOG_URLS}
    unknown = pkgs - known

    if known:
        print(f"Pre-fetching changelogs for: {', '.join(sorted(known))}")
        for pkg in sorted(known):
            text = fetch_changelog(pkg, CHANGELOG_URLS[pkg])
            if text:
                changelogs[pkg] = text
                print(f"  ✓  {pkg} ({len(text):,} chars plain text)")
            else:
                print(f"  ✗  {pkg} — will fall back to web_search")
    if unknown:
        print(f"No pre-fetch URL for: {', '.join(sorted(unknown))} — will use web_search")
    return changelogs


# ── JSON parsing helper ───────────────────────────────────────────────────────

def _parse_json(text: str) -> dict | list | None:
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


# ── Claude call (batch per package) ──────────────────────────────────────────

def remediate_package(
    client: anthropic.Anthropic,
    pkg: str,
    entries: list[dict],
    changelog: str | None = None,
) -> list[dict] | None:
    """
    Process all stale entries for one package in a single Claude call.
    Returns a list of remediation dicts (one per entry, same order),
    or None on failure.
    """
    today   = datetime.date.today().isoformat()
    n       = len(entries)
    plural  = "entries" if n > 1 else "entry"

    entries_block = "\n\n".join(
        f"Entry {i+1} (fn=\"{e['fn']}\"):\n{json.dumps(e, indent=2)}"
        for i, e in enumerate(entries)
    )

    if changelog:
        changelog_block = (
            f"\nOFFICIAL CHANGELOG for {pkg} (pre-fetched plain text — "
            f"use this directly, do NOT call web_search):\n"
            f"<changelog>\n{changelog}\n</changelog>\n"
        )
        tools              = []
        research_note      = "Use the pre-fetched changelog above. Do NOT call web_search."
    else:
        changelog_block    = ""
        tools              = [{"type": "web_search_20250305", "name": "web_search"}]
        research_note      = (
            f"Use web_search to fetch the {pkg} changelog before deciding."
        )

    user_message = f"""\
Review the following {n} stale database {plural} for the R package '{pkg}'.
Apply the full decision framework to each entry independently.
Today's date: {today}

{research_note}
{changelog_block}
{entries_block}

Return a JSON object with a "remediations" array — one item per entry in
the same order, no prose, no markdown fences:
{{
  "remediations": [
    {{
      "fn": "<function name>",
      "action": "raise_floor|extend_ceiling|close|no_change",
      "rationale": "2-3 sentences: exact changelog evidence (version+date), who is at risk, why this action.",
      "corrected_entry": {{ ...full entry... }} or null
    }}
  ]
}}
"""

    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS * max(1, n),  # scale with number of entries
        tools=tools,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw         = "\n".join(text_blocks).strip()
    parsed      = _parse_json(raw)

    if parsed is None or "remediations" not in parsed:
        # Follow-up turn
        print(f"  ↩  No valid JSON for {pkg} batch — requesting follow-up")
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": (
                "Output your final answer as a raw JSON object only — "
                "no prose, no markdown fences:\n"
                '{"remediations": [{"fn": "...", "action": "...", '
                '"rationale": "...", "corrected_entry": {...} or null}]}'
            )
        })
        response2   = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS * max(1, n),
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text_blocks2 = [b.text for b in response2.content if b.type == "text"]
        raw2         = "\n".join(text_blocks2).strip()
        parsed       = _parse_json(raw2)

    if parsed is None or "remediations" not in parsed:
        print(f"  ✗  Could not parse batch response for {pkg}")
        return None

    return parsed["remediations"]


# ── File path helpers ─────────────────────────────────────────────────────────

def version_to_dashes(version: str) -> str:
    return version.replace(".", "-")


def old_filepath(entries_dir: Path, pkg: str, fn: str, from_version: str) -> Path | None:
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
    repo     = gh.get_repo(repo_name)
    base_sha = repo.get_branch(base).commit.sha

    try:
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)
    except GithubException as e:
        if e.status == 422:
            print(f"  Branch {branch} already exists, reusing.")
        else:
            raise

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

    for path in (files_to_delete or []):
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.delete_file(
                path=path, message=f"chore: remove {path}",
                sha=existing.sha, branch=branch,
            )
        except GithubException:
            pass

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

_SCHEMA_FIELDS   = frozenset({
    "pkg", "fn", "from_version", "to_version", "risk",
    "description", "reference", "added_by", "added_date", "closed",
})
_STALENESS_ONLY  = frozenset({"key", "status", "gap", "current_version"})


# ── Entry enrichment ──────────────────────────────────────────────────────────

def enrich_entry(raw_entry: dict, entries_dir: Path) -> dict:
    """
    Merge stale entry (8 columns from check_db_staleness) with the full
    JSON from disk (which includes description, reference, risk etc.).
    Strips staleness-only fields so they never reach Claude or corrected_entry.
    """
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

    return {k: v for k, v in entry.items() if k not in _STALENESS_ONLY}


# ── Process one remediation result ───────────────────────────────────────────

def process_remediation(
    raw_entry: dict,
    entry: dict,
    remediation: dict,
    entries_dir: Path,
    gh_client,
    repo_obj,
    open_pr_titles: set[str],
    args,
    results: dict,
) -> None:
    """Apply a single remediation result: validate, build PR, update results."""
    key    = f"{entry['pkg']}::{entry['fn']}"
    action = remediation.get("action", "no_change")

    print(f"  Action:    {action}")
    print(f"  Rationale: {remediation.get('rationale', '')}")

    if action == "no_change":
        print(f"  → No change needed.\n")
        results["skipped"].append(key)
        return

    if args.dry_run.lower() == "true":
        print(f"  → Dry run: would open PR.\n")
        results["success"].append(key)
        return

    # ── Resolve corrected entry ─────────────────────────────────────────────
    corrected      = remediation.get("corrected_entry")
    existing_path  = old_filepath(
        entries_dir, raw_entry["pkg"], raw_entry["fn"], raw_entry["from_version"]
    )

    if action == "close" and not corrected:
        if existing_path and existing_path.exists():
            with open(existing_path) as f:
                corrected = json.load(f)
            print(f"  ↩  close: derived from {existing_path.name}")
        else:
            corrected = {k: v for k, v in entry.items() if k not in _STALENESS_ONLY}
            print("  ↩  close: no disk file; using enriched entry as base")

    if not corrected:
        print(f"  ✗  No corrected entry for '{action}' — skipping\n")
        results["failed"].append(key)
        return

    # Required field guard
    for field in ("pkg", "fn", "from_version", "to_version",
                  "risk", "description", "reference"):
        if field not in corrected:
            print(f"  ✗  corrected_entry missing '{field}' — skipping\n")
            results["failed"].append(key)
            return

    pkg      = corrected["pkg"]
    fn       = corrected["fn"]
    new_from = (
        raw_entry["from_version"] if action == "close"
        else corrected["from_version"]
    )
    new_path     = f"entries/{pkg}/{pkg}__{fn}__{version_to_dashes(new_from)}.json"
    old_path_obj = old_filepath(entries_dir, pkg, fn, raw_entry["from_version"])
    old_path     = f"entries/{pkg}/{old_path_obj.name}" if old_path_obj else None

    files_to_delete = (
        [old_path]
        if old_path and old_path != new_path and action == "raise_floor"
        else []
    )

    # Duplicate PR check
    safe_key = f"{pkg}-{fn}".replace("::", "-").replace(".", "-")
    branch   = f"fix/db-staleness-{safe_key}"

    if any(f"{pkg}::{fn}" in t for t in open_pr_titles):
        print(f"  → Open PR already exists — skipping\n")
        results["skipped"].append(key)
        return

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
        print(f"  ✓  PR: {pr_url}\n")
        open_pr_titles.add(pr_title)
        results["success"].append(key)
    except Exception as e:
        print(f"  ✗  Failed to create PR: {e}\n")
        results["failed"].append(key)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="reproducr-db staleness remediation agent"
    )
    parser.add_argument("--stale-file",    required=True)
    parser.add_argument("--entries-dir",   required=True)
    parser.add_argument("--dry-run",       default="false")
    parser.add_argument("--repo",          required=True)
    parser.add_argument("--fail-on-error", default="false")
    args = parser.parse_args()

    fail_on_error = args.fail_on_error.lower() == "true"
    entries_dir   = Path(args.entries_dir)
    dry_run       = args.dry_run.lower() == "true"

    with open(args.stale_file) as f:
        raw_stale_entries = json.load(f)

    if not raw_stale_entries:
        print("No stale entries to remediate.")
        sys.exit(0)

    n_total = len(raw_stale_entries)
    print(f"Remediating {n_total} stale entries "
          f"({'dry run' if dry_run else 'live'})...\n")

    # ── Pre-fetch changelogs ────────────────────────────────────────────────
    unique_pkgs = {e["pkg"] for e in raw_stale_entries}
    changelogs  = prefetch_changelogs(unique_pkgs)
    print()

    # ── Enrich entries from disk ────────────────────────────────────────────
    enriched_entries = [enrich_entry(e, entries_dir) for e in raw_stale_entries]

    # ── Group by package ────────────────────────────────────────────────────
    pkg_groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for raw, enriched in zip(raw_stale_entries, enriched_entries):
        pkg_groups[enriched["pkg"]].append((raw, enriched))

    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gh_client = (
        Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
        if not dry_run else None
    )

    # Pre-load open PR titles for duplicate detection
    open_pr_titles: set[str] = set()
    if not dry_run and gh_client:
        repo_obj = gh_client.get_repo(args.repo)
        open_pr_titles = {pr.title for pr in repo_obj.get_pulls(state="open")}
    else:
        repo_obj = None

    results = {"success": [], "skipped": [], "failed": []}
    n_pkgs  = len(pkg_groups)

    print(f"Processing {n_pkgs} package group(s) "
          f"({n_total} entries → {n_pkgs} API calls):\n")

    for pkg_idx, (pkg, group) in enumerate(pkg_groups.items(), 1):
        raw_entries  = [g[0] for g in group]
        enr_entries  = [g[1] for g in group]
        n_pkg        = len(group)

        print(f"[{pkg_idx}/{n_pkgs}] {pkg} — {n_pkg} entr{'y' if n_pkg == 1 else 'ies'}")

        remediations = remediate_package(
            anthropic_client,
            pkg,
            enr_entries,
            changelog=changelogs.get(pkg),
        )

        if remediations is None:
            print(f"  ✗  Batch call failed for {pkg} — marking all as failed\n")
            for raw, _ in group:
                results["failed"].append(f"{raw['pkg']}::{raw['fn']}")
            continue

        # Match remediations back to entries by fn
        rem_by_fn = {r.get("fn"): r for r in remediations if isinstance(r, dict)}

        for raw, enr in group:
            fn  = enr["fn"]
            key = f"{pkg}::{fn}"
            print(f"→ {key} ({raw['status']})")

            rem = rem_by_fn.get(fn)
            if rem is None:
                print(f"  ✗  No remediation returned for {fn} — skipping\n")
                results["failed"].append(key)
                continue

            process_remediation(
                raw_entry     = raw,
                entry         = enr,
                remediation   = rem,
                entries_dir   = entries_dir,
                gh_client     = gh_client,
                repo_obj      = repo_obj,
                open_pr_titles= open_pr_titles,
                args          = args,
                results       = results,
            )

        print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print("─" * 60)
    print(f"Complete: {len(results['success'])} PRs opened, "
          f"{len(results['skipped'])} skipped, "
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