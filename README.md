# reproducr-db

Community-maintained database of known silent breaking changes in R packages,
used by [`reproducr`](https://github.com/repro-stats/reproducr) to power
`risk_score()`.

<!-- badges: start -->
[![Validate entries](https://github.com/repro-stats/reproducr-db/actions/workflows/validate.yml/badge.svg)](https://github.com/repro-stats/reproducr-db/actions/workflows/validate.yml)
<!-- badges: end -->

---

## What is this?

Each entry documents a case where a package update **silently changed a
function's behaviour** — not its interface — in a way that can alter
analytical results without producing an error or warning.

This database is compiled into `reproducr` at each release. When you call
`risk_score()`, it checks your code against these entries.

---

## Entry format

Each entry is a JSON file in `entries/{pkg}/`:

```json
{
  "pkg":          "dplyr",
  "fn":           "group_by",
  "from_version": "1.0.99",
  "to_version":   "1.1.9",
  "risk":         "medium",
  "description":  "In dplyr 1.1.0, arrange() and group_by() changed to order character vectors using the C locale instead of the system locale ...",
  "reference":    "https://dplyr.tidyverse.org/reference/group_by.html",
  "added_by":     "ndohpenngit",
  "added_date":   "2026-06-01"
}
```

### Filename convention

```
entries/{pkg}/{pkg}__{fn}__{from_version}.json
```

Examples:
```
entries/dplyr/dplyr__group_by__1-0-99.json
entries/stats/stats__sample__3-5-99.json
entries/rstan/rstan__stan__2-21-99.json
```

---

## Version window design principles

Each entry uses a half-open interval `(from_version, to_version]`:

```
installed > from_version  AND  installed <= to_version  →  flagged
```

**`from_version`** — the last version where the old behaviour applied.
Set to one patch version below the first risky version (e.g. `"1.0.99"` if
the change was in `1.1.0`).

**`to_version`** — where careful judgement is required. The window should
close when the ecosystem has moved on.

| Change type | `to_version` strategy |
|---|---|
| Permanent package behaviour change | Version ceiling of current series (e.g. `"1.1.9"`) |
| Historical base R change (pre-2020) | Close at the patch series (e.g. `"3.6.9"`) |
| Recent base R change (post-2022) | Keep open with modest ceiling (e.g. `"4.3.9"`) |
| Fixed in a later version | Set to the last affected version exactly |
| Ongoing / never fixed | Set to current series ceiling, revisit periodically |

**Rule: a missed flag is better than a false positive.** False positives
erode trust in the tool. Prefer narrower windows when in doubt.

---

## Risk levels

| Level | When to use |
|---|---|
| `"high"` | Output *values* change silently — model coefficients, table cells, random draws, sort order. Any result that goes into a paper could be different. |
| `"medium"` | An argument was renamed or deprecated; the function may warn, error, or produce different output depending on the call pattern. |
| `"low"` | A minor behavioural note. Output unlikely to differ in practice, but worth flagging. |

---

## Current coverage

Browse all entries in [`entries/`](entries/) — one subdirectory per package,
one JSON file per tracked function. The CI badge above confirms all entries
are currently valid against the schema.

---

## Contributing

### Adding a new entry

1. Fork this repository
2. Create a JSON file in `entries/{pkg}/` following the naming convention
3. Validate it locally:
   ```bash
   pip install jsonschema
   python3 -c "
   import json, jsonschema
   schema = json.load(open('schema.json'))
   entry  = json.load(open('entries/mypkg/mypkg__myfn__x-x-xx.json'))
   jsonschema.validate(entry, schema)
   print('Valid')
   "
   ```
4. Open a PR — the CI will validate automatically

### What makes a good entry

1. **A specific function in a specific package** changed its output between versions
2. The change is **silent** — no error or warning on the old calling pattern
3. The change can **affect analytical conclusions** — not just cosmetic differences
4. The change is **documented** — there is an official `NEWS.md` or changelog entry
   and the PR must quote the relevant excerpt

### What does not belong

- Changes that produce visible errors (not silent)
- Cosmetic changes to printed output only
- Performance changes with no effect on results
- Changes without official documentation
- New arguments or features that are purely additive (existing code unaffected)

---

## How entries reach `reproducr`

When `reproducr` prepares a release, maintainers run:

```r
Rscript data-raw/sync_db.R
```

This compiles all JSON entries into `R/breaking_changes_db.R` in the
`reproducr` package. The compiled database ships with each release.

---

## Keeping entries current

As packages release new versions, entries may become stale — their
`to_version` ceiling falls below the current CRAN release.

The `reproducr` package runs `check_db_staleness()` weekly and opens
an issue in this repository when stale entries are detected. If you
notice a stale entry, a PR to update `to_version` is a valuable
contribution even without adding a new entry.

---

## Schema

The full JSON schema is in [`schema.json`](schema.json). Every PR is
validated automatically against it.