#!/usr/bin/env python3
"""Download Helm charts and produce static upgrade comparison reports."""

import argparse
import difflib
import hashlib
import html
import json
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import yaml


RELEASE_BASE = "https://github.com/prometheus-community/helm-charts/releases/download"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
MAX_MEMBERS = 20000
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
CHART_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MISSING = object()


def normalize_version(version, chart):
    version = version.removeprefix(chart + "-")
    if not VERSION.fullmatch(version):
        raise ValueError(f"Invalid version {version!r}; use a concrete version such as 25.0.0")
    return version


def release_url(chart, version):
    if not CHART_NAME.fullmatch(chart):
        raise ValueError("Chart name must contain lowercase letters, digits, and hyphens")
    version = normalize_version(version, chart)
    tag = f"{chart}-{version}"
    return f"{RELEASE_BASE}/{tag}/{tag}.tgz"


def download_chart(chart, version, destination):
    url = release_url(chart, version)
    destination = Path(destination)
    try:
        with urlopen(url, timeout=60) as response, destination.open("xb") as output:
            total = 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Chart download exceeds 100 MiB limit")
                output.write(chunk)
    except HTTPError as exc:
        raise ValueError(f"Cannot download {url}: HTTP {exc.code}; verify the release exists") from exc
    except URLError as exc:
        raise ValueError(f"Cannot download {url}: {exc.reason}") from exc
    return {"url": url, "sha256": file_info(destination)["sha256"]}


def extract_chart(archive, destination, chart):
    """Accept only regular files/directories confined to the chart root."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as package:
        members = []
        seen = set()
        total = 0
        for member in package:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] != chart
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe archive member: {member.name!r}")
            if path in seen:
                raise ValueError(f"Duplicate archive member: {member.name!r}")
            seen.add(path)
            total += member.size
            if total > MAX_EXTRACTED_BYTES or len(seen) > MAX_MEMBERS:
                raise ValueError("Chart archive exceeds extraction limits")
            members.append(member)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    root = destination / chart
    for name in ("Chart.yaml", "values.yaml"):
        if not (root / name).is_file():
            raise ValueError(f"Chart archive is missing {chart}/{name}")
    return root


def json_value(value, ancestors=None, depth=0, budget=None):
    """Normalize YAML scalars and reject recursive aliases before comparison."""
    ancestors = set() if ancestors is None else ancestors
    budget = [1000000] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("YAML expansion exceeds one million values")
    if depth > 100 or id(value) in ancestors:
        raise ValueError("YAML nesting is excessive or contains recursive aliases")
    if isinstance(value, (dict, list)):
        ancestors = ancestors | {id(value)}
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key = str(key)
                if key in result:
                    raise ValueError(f"YAML keys collide after conversion: {key}")
                result[key] = json_value(item, ancestors, depth + 1, budget)
            return result
        return [json_value(item, ancestors, depth + 1, budget) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), -float("inf")):
            return str(value)
        return value
    return str(value)


def read_yaml(path):
    value = json_value(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return value


def pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def deep_diff(old, new, path=()):
    """Diff mappings and lists, preserving missing vs null and scalar types."""
    if old is not MISSING and new is not MISSING and type(old) is type(new):
        if isinstance(old, dict):
            changes = []
            for key in sorted(old.keys() | new.keys()):
                changes.extend(deep_diff(old.get(key, MISSING), new.get(key, MISSING), path + (key,)))
            return changes
        if isinstance(old, list):
            changes = []
            for index in range(max(len(old), len(new))):
                changes.extend(deep_diff(
                    old[index] if index < len(old) else MISSING,
                    new[index] if index < len(new) else MISSING, path + (index,)))
            return changes
        if old == new:
            return []
    # Expand added/removed containers so every configuration key is visible.
    existing = new if old is MISSING else old
    if (old is MISSING or new is MISSING) and isinstance(existing, (dict, list)) and existing:
        items = sorted(existing.items()) if isinstance(existing, dict) else enumerate(existing)
        changes = []
        for key, value in items:
            changes.extend(deep_diff(MISSING if old is MISSING else value,
                                     value if old is MISSING else MISSING, path + (key,)))
        return changes
    change = {"path": pointer(path), "kind": "added" if old is MISSING else
              "removed" if new is MISSING else "modified"}
    if old is not MISSING:
        change["old"] = old
    if new is not MISSING:
        change["new"] = new
    if old is not MISSING and new is not MISSING and type(old) is not type(new):
        change["type_changed"] = True
    return [change]


def file_info(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def inventory(root):
    return {path.relative_to(root).as_posix(): file_info(path)
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


def unified_diff(old_path, new_path, name):
    old = old_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if old_path.is_file() else []
    new = new_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if new_path.is_file() else []
    return "".join(line if line.endswith("\n") else line + "\n"
                   for line in difflib.unified_diff(old, new, fromfile=f"old/{name}", tofile=f"new/{name}"))


def compare_files(old_root, new_root):
    old, new = inventory(old_root), inventory(new_root)
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    modified = sorted(name for name in old.keys() & new.keys() if old[name] != new[name])
    unchanged = len(old.keys() & new.keys()) - len(modified)
    return {
        "counts": {"old": len(old), "new": len(new), "added": len(added),
                   "removed": len(removed), "modified": len(modified), "unchanged": unchanged},
        "added": [{"path": name, "new": new[name]} for name in added],
        "removed": [{"path": name, "old": old[name]} for name in removed],
        "modified": [{"path": name, "old": old[name], "new": new[name]} for name in modified],
    }


def compare_dependencies(old, new):
    def keyed(metadata):
        result = {}
        dependencies = metadata.get("dependencies")
        if dependencies is None:
            dependencies = []
        if not isinstance(dependencies, list):
            raise ValueError("Chart.yaml dependencies must be a list of mappings")
        for dep in dependencies:
            if not isinstance(dep, dict) or not isinstance(dep.get("name"), str) or not dep["name"]:
                raise ValueError("Each Chart.yaml dependency must be a mapping with a nonempty name")
            if "alias" in dep and (not isinstance(dep["alias"], str) or not dep["alias"]):
                raise ValueError("Chart.yaml dependency aliases must be nonempty strings")
            key = dep.get("alias") or dep["name"]
            if key in result:
                raise ValueError(f"Duplicate dependency name/alias: {key}")
            result[key] = dep
        return result

    old, new = keyed(old), keyed(new)
    result = []
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        entry = {"name": name, "kind": "added" if before is None else
                 "removed" if after is None else "modified", "old": before, "new": after,
                 "major_bump": False, "version_comparison": "not_applicable"}
        if before and after and before.get("version") != after.get("version"):
            a, b = str(before.get("version", "")), str(after.get("version", ""))
            if VERSION.fullmatch(a) and VERSION.fullmatch(b):
                entry["major_bump"] = int(b.split(".")[0]) > int(a.split(".")[0])
                entry["version_comparison"] = "exact"
            elif all(VERSION.fullmatch(v) or re.fullmatch(r"\d+\.(?:\d+|[xX*])\.[xX*]", v)
                     for v in (a, b)):
                entry["major_bump"] = int(b.split(".")[0]) > int(a.split(".")[0])
                entry["version_comparison"] = "fixed_major_range"
            else:
                entry["version_comparison"] = "range_or_non_semver_manual_review"
        result.append(entry)
    return result


def required_schema_paths(root):
    path = root / "values.schema.json"
    if not path.is_file():
        return set()
    schema = json.loads(path.read_text(encoding="utf-8"))
    found = set()

    def visit(node, parts):
        if not isinstance(node, dict):
            return
        for key in node.get("required", []):
            found.add(pointer(parts + (key,)))
        for key, child in node.get("properties", {}).items():
            visit(child, parts + (key,))

    visit(schema, ())
    return found


def required_template_calls(root):
    found = set()
    for path in root.rglob("*"):
        if path.is_file() and "templates" in path.relative_to(root).parts:
            text = path.read_text(encoding="utf-8", errors="replace")
            for action in re.findall(r"{{-?(.*?)-?}}", text, re.DOTALL):
                if re.search(r"\brequired\b", action):
                    for value in re.findall(r"\.Values((?:\.[A-Za-z0-9_-]+)+)", action):
                        found.add((path.relative_to(root).as_posix(),
                                   pointer(tuple(value.lstrip(".").split(".")))))
    return found


def customization_impacts(custom, changes):
    leaves = deep_diff({}, custom)
    impacts = []
    for leaf in leaves:
        path = leaf["path"]
        affected = [change for change in changes
                    if path == change["path"] or path.startswith(change["path"] + "/")
                    or change["path"].startswith(path + "/")]
        if affected:
            impacts.append({"path": path, "changes": affected})
    return impacts


def assess_risk(old_root, new_root, values, templates, metadata, dependencies, custom=None, crds=None):
    findings = []

    def flag(severity, category, message, evidence):
        findings.append({"severity": severity, "category": category,
                         "message": message, "evidence": evidence})

    removed = [v for v in values if v["kind"] == "removed"]
    types = [v for v in values if v.get("type_changed")]
    if removed:
        flag("high", "removed_values", "Removed keys may invalidate existing overrides.", removed)
    if types:
        flag("high", "value_type_changes", "Changed types may break existing overrides.", types)
    schema_required = sorted(required_schema_paths(new_root) - required_schema_paths(old_root))
    if schema_required:
        flag("high", "new_schema_requirements",
             "New schema-required fields; defaults or parent-object conditions may already satisfy them.",
             schema_required)
    template_required = sorted(required_template_calls(new_root) - required_template_calls(old_root))
    if template_required:
        flag("high", "new_template_requirements",
             "New required-call candidates; paths are relative to the owning chart and calls may be conditional.",
             [{"template": template, "path": path} for template, path in template_required])
    for dependency in dependencies:
        flag("high" if dependency["major_bump"] or dependency["kind"] == "removed" else "medium",
             "dependency_change", "Review dependency overrides and upgrade instructions.", dependency)
    for template in templates:
        logic = [line for line in template["diff"].splitlines()
                 if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                 and ("{{" in line or re.search(r"\b(apiVersion|kind):", line))]
        flag("high" if template["kind"] == "removed" else "medium", "template_change",
             "Template changes may conflict with custom templates or alter deployed resources.",
             {"path": template["path"], "kind": template["kind"], "logic_changes": logic})
    for crd in crds or []:
        flag("high", "crd_change",
             "Review CRD schemas and existing custom resources; Helm does not automatically upgrade/delete CRDs.",
             {"path": crd["path"], "kind": crd["kind"]})
    old_version, new_version = str(metadata["old"].get("version", "")), str(metadata["new"].get("version", ""))
    if VERSION.fullmatch(old_version) and VERSION.fullmatch(new_version):
        if int(new_version.split(".")[0]) > int(old_version.split(".")[0]):
            flag("high", "chart_major_bump", "Chart major version increased; review upstream migration notes.",
                 {"old": old_version, "new": new_version})
    for change in metadata["changes"]:
        if change["path"] in ("/kubeVersion", "/apiVersion", "/type", "/appVersion"):
            flag("medium", "metadata_compatibility", "Review chart/application compatibility.", change)
    notices = []
    for filename in ("values.yaml", "README.md", "Chart.yaml"):
        diff = unified_diff(old_root / filename, new_root / filename, filename)
        notices.extend({"file": filename, "line": line[1:]} for line in diff.splitlines()
                       if line.startswith("+") and not line.startswith("+++")
                       and re.search(r"\b(deprecat\w*|breaking|migration)\b", line, re.I))
    if notices:
        flag("medium", "deprecation_notices", "New upstream deprecation/migration notices (text heuristic).", notices)
    impacts = customization_impacts(custom, values) if custom is not None else []
    if impacts:
        flag("high", "custom_values_overlap", "Supplied overrides overlap changed defaults; review compatibility.", impacts)
    if any(v["kind"] != "removed" for v in values):
        flag("medium", "changed_defaults", "New or changed defaults can alter behavior even without overrides.",
             [v["path"] for v in values if v["kind"] != "removed"])
    return {
        "level": "high" if any(f["severity"] == "high" for f in findings) else "medium" if findings else "low",
        "findings": findings, "custom_values_checked": custom is not None, "customization_impacts": impacts,
        "limitations": [
            "Static analysis flags potential breaking changes, not proven runtime failures.",
            "Added defaults are not automatically required. Schema requirements and template required calls are separate signals.",
            "Schema analysis covers explicit properties/required only, not $ref, combinators, arrays, or conditional schemas.",
            "Template required-call detection is heuristic; dynamic/indexed lookups and helper indirection may be missed.",
            "Lists are compared by index; insertions or reordering can produce cascading changes.",
            "Removed keys are detectable; deprecations are only flagged when present in new upstream notices.",
            "Complex dependency ranges require manual review; bundled subchart archives are hashed, not recursively unpacked.",
            "No Helm rendering, Kubernetes validation, or three-way merge with customized templates is performed.",
            "Review upstream migration guides and render/test with your overrides before deployment.",
        ],
    }


def compare_charts(old_root, new_root, custom_values=None):
    old_root, new_root = Path(old_root), Path(new_root)
    files = compare_files(old_root, new_root)
    values = deep_diff(read_yaml(old_root / "values.yaml"), read_yaml(new_root / "values.yaml"))
    old_metadata, new_metadata = read_yaml(old_root / "Chart.yaml"), read_yaml(new_root / "Chart.yaml")
    metadata = {"old": old_metadata, "new": new_metadata, "changes": deep_diff(old_metadata, new_metadata)}
    dependencies = compare_dependencies(old_metadata, new_metadata)
    templates, crds = [], []
    for kind in ("added", "removed", "modified"):
        for entry in files[kind]:
            name = entry["path"]
            parts = PurePosixPath(name).parts
            if "templates" in parts or "crds" in parts:
                changes = templates if "templates" in parts else crds
                changes.append({"path": name, "kind": kind,
                                "diff": unified_diff(old_root / name, new_root / name, name)})
    lock = {"old": read_yaml(old_root / "Chart.lock") if (old_root / "Chart.lock").is_file() else None,
            "new": read_yaml(new_root / "Chart.lock") if (new_root / "Chart.lock").is_file() else None}
    lock["changes"] = deep_diff(lock["old"], lock["new"])
    schema_diff = unified_diff(old_root / "values.schema.json", new_root / "values.schema.json", "values.schema.json")
    return {
        "report_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files, "values": values, "templates": sorted(templates, key=lambda t: t["path"]),
        "crds": sorted(crds, key=lambda c: c["path"]),
        "metadata": metadata, "dependencies": dependencies, "dependency_lock": lock,
        "schema_diff": schema_diff,
        "risk": assess_risk(old_root, new_root, values, templates, metadata, dependencies, custom_values, crds),
    }


def summary_text(report):
    old, new = report["metadata"]["old"], report["metadata"]["new"]
    counts = report["files"]["counts"]
    lines = [
        f"Helm comparison: {old.get('name')} {old.get('version')} -> {new.get('name')} {new.get('version')}",
        f"Generated: {report['generated_at']}",
        f"Files: {counts['old']} old, {counts['new']} new; {counts['added']} added, "
        f"{counts['removed']} removed, {counts['modified']} modified, {counts['unchanged']} unchanged",
        f"Values: {len(report['values'])} changes",
        f"Templates: {len(report['templates'])} changes",
        f"CRDs: {len(report['crds'])} changes",
        f"Dependencies: {len(report['dependencies'])} changes",
        f"Risk: {report['risk']['level'].upper()} (static analysis)",
        "", "Potential breaking changes and customization risks:",
    ]
    grouped = {}
    for finding in report["risk"]["findings"]:
        key = (finding["severity"], finding["category"], finding["message"])
        grouped[key] = grouped.get(key, 0) + 1
    for (severity, category, message), count in grouped.items():
        lines.append(f"- [{severity.upper()}] {category} ({count} {'finding' if count == 1 else 'findings'}): {message}")
    if not report["risk"]["findings"]:
        lines.append("- No static risk signals detected; this is not a deployment safety guarantee.")
    lines.extend(["", "Limitations:"] + [f"- {item}" for item in report["risk"]["limitations"]])
    return "\n".join(lines) + "\n"


def highlighted_diff(diff):
    lines = []
    for line in diff.splitlines():
        css = "addition" if line.startswith("+") else "deletion" if line.startswith("-") else "hunk" if line.startswith("@@") else ""
        lines.append(f'<span class="{css}">{html.escape(line)}</span>')
    return '<pre class="diff"><code>' + "\n".join(lines) + "</code></pre>"


def report_html(report):
    def data_section(title, value):
        text = json.dumps(value, indent=2, ensure_ascii=False)
        tokens = re.compile(r'("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false|null)\b|(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b')
        parts, end = [], 0
        for match in tokens.finditer(text):
            parts.append(html.escape(text[end:match.start()]))
            css = "key" if match[2] else "string" if match[1] else "literal"
            token = match[1] if match[1] is not None else match[0]
            parts.append(f'<span class="{css}">{html.escape(token)}</span>{match[2] or ""}')
            end = match.end()
        parts.append(html.escape(text[end:]))
        return f"<section><h2>{html.escape(title)}</h2><pre><code>{''.join(parts)}</code></pre></section>"

    title = f"{report['metadata']['old'].get('name')} chart comparison"
    sections = [f"<h1>{html.escape(title)}</h1><pre>{html.escape(summary_text(report))}</pre>"]
    for heading, key in (("Download provenance", "sources"), ("File structure and SHA256", "files"),
                         ("Values: key-by-key changes (JSON Pointer paths)", "values"),
                         ("Chart metadata", "metadata"), ("Dependency changes", "dependencies"),
                         ("Dependency lock", "dependency_lock"), ("Risk assessment", "risk")):
        if key in report:
            sections.append(data_section(heading, report[key]))
    for title, key in (("Template diffs", "templates"), ("CRD diffs", "crds")):
        sections.append(f"<section><h2>{title}</h2>")
        for change in report[key]:
            sections.append(f"<details open><summary>{html.escape(change['kind'] + ': ' + change['path'])}</summary>"
                            + highlighted_diff(change["diff"]) + "</details>")
        sections.append("</section>")
    sections.append("<section><h2>Values schema diff</h2>" + highlighted_diff(report["schema_diff"]) + "</section>")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Helm chart comparison</title><style>
body{font:16px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#202124}
pre{overflow:auto;padding:1rem;background:#f5f6f8;border:1px solid #ddd;white-space:pre-wrap;overflow-wrap:anywhere}
summary{cursor:pointer;font-weight:bold}.addition{color:#116329;background:#dafbe1}
.deletion{color:#82071e;background:#ffebe9}.hunk,.key{color:#0550ae}
.string{color:#0a3069}.literal{color:#8250df}section{margin:2rem 0}
</style></head><body>""" + "\n".join(sections) + "</body></html>\n"


def write_reports(report, output):
    output = Path(output)
    paths = []
    for suffix, content in (("json", json.dumps(report, indent=2, ensure_ascii=False) + "\n"),
                            ("html", report_html(report)), ("txt", summary_text(report))):
        path = output / f"report.{suffix}"
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_version", help="Old version, e.g. 25.0.0 or prometheus-25.0.0")
    parser.add_argument("new_version", help="New version, e.g. 29.37.0")
    parser.add_argument("--chart", default="prometheus", help="Chart in prometheus-community/helm-charts")
    parser.add_argument("--output", type=Path, default=Path("output"), help="Report parent directory (default: output/)")
    parser.add_argument("--custom-values", type=Path, help="Optional YAML overrides to assess against changed defaults")
    args = parser.parse_args(argv)
    run = None
    try:
        old_version = normalize_version(args.old_version, args.chart)
        new_version = normalize_version(args.new_version, args.chart)
        release_url(args.chart, old_version)
        custom = read_yaml(args.custom_values) if args.custom_values else None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run = args.output.resolve() / f"{args.chart}-{old_version}-to-{new_version}-{timestamp}"
        run.mkdir(parents=True, exist_ok=False)
        sources, roots = {}, []
        for label, version in (("old", old_version), ("new", new_version)):
            archive = run / f"{label}-{args.chart}-{version}.tgz"
            print(f"Downloading {args.chart} {version}...", flush=True)
            sources[label] = download_chart(args.chart, version, archive)
            root = extract_chart(archive, run / label, args.chart)
            metadata = read_yaml(root / "Chart.yaml")
            if metadata.get("name") != args.chart or str(metadata.get("version")) != version:
                raise ValueError(f"{label} archive Chart.yaml does not match requested chart/version")
            roots.append(root)
        report = compare_charts(*roots, custom_values=custom)
        report["sources"] = sources
        paths = write_reports(report, run)
        print(summary_text(report))
        for path in paths:
            print(f"Report: {path}")
        return 0
    except (OSError, ValueError, yaml.YAMLError, tarfile.TarError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if run:
            print(f"Incomplete run (no valid report): {run}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
