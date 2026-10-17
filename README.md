# helm-chart-comparison

Download and compare two Helm chart releases from
[`prometheus-community/helm-charts`](https://github.com/prometheus-community/helm-charts).
Produces a detailed JSON report, a self-contained syntax-highlighted HTML report,
and a quick text summary. No Helm binary or Kubernetes cluster is required.

## Quick start

Requires Python 3.10+.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python helm_compare.py 25.0.0 29.37.0
```

These are concrete, verified releases. **Date correction:** `prometheus-25.0.0`
was published September 19, **2023**, not September 2025; it is a representative
25.x baseline, not a claim of being one year old. `prometheus-29.37.0` was published
October 14, 2026. `25.x.x` is a placeholder, not a downloadable version.
The tool compares exactly the versions supplied and does not resolve `latest`.

Downloads use direct release asset URLs:

```text
https://github.com/prometheus-community/helm-charts/releases/download/prometheus-25.0.0/prometheus-25.0.0.tgz
https://github.com/prometheus-community/helm-charts/releases/download/prometheus-29.37.0/prometheus-29.37.0.tgz
```

## Usage

```sh
python helm_compare.py OLD_VERSION NEW_VERSION [--chart CHART] [--output DIRECTORY] [--custom-values FILE]

# Release tags are also accepted:
python helm_compare.py prometheus-25.0.0 prometheus-29.37.0

# Assess your overrides against changes:
python helm_compare.py 25.0.0 29.37.0 --custom-values my-values.yaml

# Other charts in the same repository:
python helm_compare.py 60.0.0 61.0.0 --chart kube-prometheus-stack
```

Versions are positional: changes are always **old → new**. The default chart is
`prometheus`; use `--chart` for another chart from this repository.
Run `python helm_compare.py --help` for options.

Each invocation creates a separate UTC-timestamped directory under `output/`
(or `--output`), containing:

```text
output/prometheus-25.0.0-to-29.37.0-<timestamp>/
  old-prometheus-25.0.0.tgz
  new-prometheus-29.37.0.tgz
  old/prometheus/...
  new/prometheus/...
  report.json
  report.html
  report.txt
```

Open `report.html` directly in a browser; it requires no network access or
external JavaScript. Console output includes summary statistics and report paths.
Downloads and generated output are ignored by Git. Reports can contain chart
defaults and configuration details: treat them as potentially sensitive.
Custom override values are not copied into reports; overlapping paths and
upstream changes are recorded.

## Report contents

* **Files:** added, removed, modified, and unchanged counts; paths, byte sizes,
  and SHA256 hashes for every changed file, including non-template files.
* **Values:** recursive key-by-key changes, including list indexes, additions,
  removals, null values, empty containers, and scalar type changes.
  Paths use [JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) notation:
  `/server/port`, with `~1` escaping `/` and `~0` escaping `~` in key names.
  A type replacement includes the complete old and new values.
* **Templates:** complete unified diffs for every added, removed, or modified
  file under any `templates/` directory, including helpers, NOTES, and unpacked
  dependency templates, not only `.yaml` files.
* **CRDs:** separate unified diffs and high-risk findings for changed files under
  `crds/` directories, including unpacked dependencies.
* **Metadata:** complete old/new `Chart.yaml` and recursive field differences.
* **Dependencies:** additions/removals and all changed dependency fields,
  matched by alias or name; semantic major-version increases are flagged for
  exact versions and fixed-major wildcards such as `1.6.*` → `2.1.1`.
  Complex version ranges are marked for manual review. `Chart.lock` is
  compared separately, and schema text changes are included.
* **Risks:** removed configuration keys, type changes, new explicit schema
  requirements, new template `required`-call candidates, changed defaults,
  chart/dependency major bumps, compatibility metadata, template logic/resource
  changes, and new upstream deprecation/migration notices.
  Optional `--custom-values` highlights overrides overlapping changed paths.

### Programmatic use

`report.json` has `report_version: 1` and top-level `generated_at`, `sources`,
`files`, `values`, `templates`, `crds`, `metadata`, `dependencies`, `dependency_lock`,
`schema_diff`, and `risk` fields. `sources` includes download URLs and archive
SHA256 hashes (integrity records, **not** signature verification).
Each value change has `path`, `kind` (`added`, `removed`, `modified`),
and applicable `old`/`new` fields; a missing field differs from JSON `null`.
`risk.findings` contains `severity`, `category`, `message`, and concrete
`evidence`; `risk.level` is the highest finding severity, or `low` if none.

Local, already-extracted chart directories can also be compared in Python:

```python
from helm_compare import compare_charts, write_reports
from pathlib import Path

report = compare_charts(Path("old/prometheus"), Path("new/prometheus"))
output = Path("local-reports")
output.mkdir(exist_ok=True)
write_reports(report, output)
```

## Interpretation and limits

This is a **static upgrade-review aid**, not an automatic migration or deployment
safety guarantee. A high risk means manual review is needed, not that an upgrade
will necessarily fail. New defaults are **not** automatically considered required.
Schema-required values may already have defaults, and nested requirements only
apply when their parent objects exist. Template `required` calls may be conditional.
Their evidence includes the owning template filename; value paths are local to
that template's chart, not necessarily the top-level chart.

Schema detection covers explicit `properties`/`required`, not `$ref`, combinators,
array items, or conditional schemas. Template detection cannot fully interpret
Go templates, dynamic/indexed values, helper indirection, or runtime behavior.
Lists are compared by index, so insertions or reordering may cause cascading diffs.
Removed keys are detectable; deprecated keys are only flagged through new
upstream deprecation notices, not an authoritative deprecation catalog.
Bundled `.tgz` dependencies are hashed as files, not recursively unpacked.

No charts are executed, no templates are rendered, and no three-way merge against
custom templates is attempted. Review the upstream migration guides, inspect
template diffs, and run `helm lint` / `helm template` with your overrides before
testing in a non-production cluster. Custom overrides are compared for changed
paths, not validated against a schema.

Downloads require HTTPS access to GitHub and its release-asset redirect hosts.
The CLI exits `0` on success, `1` for download, extraction, or input failures
(`2` for invalid command-line usage). Failure paths identify incomplete run
directories; partial artifacts are retained for diagnosis and can be deleted.
Archives are limited to 100 MiB compressed, 500 MiB extracted, and 20,000 members.
Absolute/traversal paths, links, special files, and duplicate members are rejected.
Only process trusted chart sources; these checks are not a general-purpose sandbox.

## Tests

Uses Python's standard-library `unittest`; no separate test framework is needed.
Tests are offline and cover diffs, risk signals, reports, archive safety, and CLI.

```sh
python -m unittest discover -s tests -v
```
