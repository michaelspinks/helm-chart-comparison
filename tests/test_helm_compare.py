import contextlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import helm_compare as compare


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        for root, version in ((self.old, "1.0.0"), (self.new, "2.0.0")):
            root.mkdir()
            self.write(root, "Chart.yaml", f"name: prometheus\nversion: {version}\n")
            self.write(root, "values.yaml", "{}\n")

    def write(self, root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_deep_diff_lists_types_null_and_escaped_keys(self):
        changes = compare.deep_diff(
            {"a": [1, 2], "null": None, "flag": True, "a/b~c": "before"},
            {"a": [1, 3, 4], "flag": 1, "a/b~c": "after", "nested": {"empty": {}}},
        )
        keyed = {item["path"]: item for item in changes}
        self.assertEqual(keyed["/a/1"]["new"], 3)
        self.assertEqual(keyed["/a/2"]["kind"], "added")
        self.assertEqual(keyed["/null"], {"path": "/null", "kind": "removed", "old": None})
        self.assertTrue(keyed["/flag"]["type_changed"])
        self.assertIn("/a~1b~0c", keyed)
        self.assertEqual(keyed["/nested/empty"]["new"], {})

    def test_full_comparison_and_reports(self):
        self.write(self.old, "values.yaml", "removed: true\nserver:\n  port: 80\n")
        self.write(self.new, "values.yaml", "server:\n  port: '8080'\nadded: null\n# deprecated: old setting\n")
        self.write(self.old, "templates/_helpers.tpl", '{{ define "name" }}old{{ end }}\n')
        self.write(self.new, "templates/_helpers.tpl", '{{ define "name" }}new{{ end }}\n')
        self.write(self.old, "templates/gone.yaml", "kind: Service\n")
        self.write(self.new, "templates/new.yaml", '{{ required "supply token" .Values.token }}\n')
        self.write(self.new, "templates/NOTES.txt", "<script>alert(1)</script>\n")
        self.write(self.old, "unchanged.txt", "identical")
        self.write(self.new, "unchanged.txt", "identical")
        self.write(self.new, "Chart.yaml", """name: prometheus
version: 2.0.0
kubeVersion: '>=1.25.0'
dependencies:
- name: exporter
  version: 2.0.0
""")
        self.write(self.old, "Chart.yaml", """name: prometheus
version: 1.0.0
dependencies:
- name: exporter
  version: 1.4.0
""")
        self.write(self.new, "values.schema.json", json.dumps(
            {"properties": {"server": {"required": ["token"]}}}))
        self.write(self.new, "Chart.lock", "dependencies:\n- name: exporter\n  version: 2.0.0\n")
        report = compare.compare_charts(self.old, self.new, {"server": {"port": 9000}})
        self.assertEqual(report["files"]["counts"],
                         {"old": 5, "new": 8, "added": 4, "removed": 1, "modified": 3, "unchanged": 1})
        self.assertEqual(len(report["templates"]), 4)
        self.assertTrue(report["dependencies"][0]["major_bump"])
        self.assertEqual(report["risk"]["level"], "high")
        findings = {f["category"]: f for f in report["risk"]["findings"]}
        self.assertEqual(findings["new_schema_requirements"]["evidence"], ["/server/token"])
        self.assertEqual(findings["new_template_requirements"]["evidence"],
                         [{"template": "templates/new.yaml", "path": "/token"}])
        self.assertIn("removed_values", findings)
        self.assertIn("value_type_changes", findings)
        self.assertIn("deprecation_notices", findings)
        self.assertEqual(report["risk"]["customization_impacts"][0]["path"], "/server/port")
        self.assertTrue(report["dependency_lock"]["changes"])
        output = self.root / "reports"
        output.mkdir()
        paths = compare.write_reports(report, output)
        self.assertEqual(len(paths), 3)
        self.assertEqual(json.loads((output / "report.json").read_text()), report)
        html = (output / "report.html").read_text()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('class="addition"', html)
        self.assertIn('class="key"', html)
        self.assertIn("Risk: HIGH", (output / "report.txt").read_text())

    def test_identical_charts_are_low_risk(self):
        report = compare.compare_charts(self.old, self.old)
        self.assertEqual(report["values"], [])
        self.assertEqual(report["templates"], [])
        self.assertEqual(report["files"]["counts"]["unchanged"], 2)
        self.assertEqual(report["risk"]["level"], "low")

    def test_dependency_aliases_ranges_and_removals(self):
        changes = compare.compare_dependencies(
            {"dependencies": [{"name": "x", "alias": "one", "version": "~1.0"},
                              {"name": "x", "alias": "two", "version": "1.0.0"}]},
            {"dependencies": [{"name": "x", "alias": "one", "version": "~2.0"}]},
        )
        self.assertEqual(changes[0]["version_comparison"], "range_or_non_semver_manual_review")
        self.assertFalse(changes[0]["major_bump"])
        self.assertEqual(changes[1]["kind"], "removed")

    def test_new_defaults_are_not_reported_as_required(self):
        self.write(self.new, "values.yaml", "optional: true\n")
        report = compare.compare_charts(self.old, self.new)
        self.assertFalse(any("requirements" in f["category"] for f in report["risk"]["findings"]))

    def test_dependency_fixed_major_wildcards(self):
        changes = compare.compare_dependencies(
            {"dependencies": [{"name": "alertmanager", "version": "1.6.*"}]},
            {"dependencies": [{"name": "alertmanager", "version": "2.1.1"}]},
        )
        self.assertTrue(changes[0]["major_bump"])
        self.assertEqual(changes[0]["version_comparison"], "fixed_major_range")

    def test_alias_expansion_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "expansion"):
            compare.json_value([1, 2, 3], budget=[2])

    def test_invalid_dependency_metadata_has_clear_errors(self):
        for dependencies in ("invalid", {}, False, 0, {"name": "x"}, ["invalid"], [{}], [{"alias": "x"}],
                             [{"name": []}], [{"name": "x", "alias": []}]):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                compare.compare_dependencies({"dependencies": dependencies}, {})

    def test_crd_changes_have_diffs_and_high_risk(self):
        self.write(self.old, "charts/crds/crds/resource.yaml",
                   "kind: CustomResourceDefinition\nspec:\n  scope: Namespaced\n")
        self.write(self.new, "charts/crds/crds/resource.yaml",
                   "kind: CustomResourceDefinition\nspec:\n  scope: Cluster\n")
        report = compare.compare_charts(self.old, self.new)
        self.assertEqual(report["templates"], [])
        self.assertEqual(len(report["crds"]), 1)
        self.assertIn("+  scope: Cluster", report["crds"][0]["diff"])
        self.assertEqual(report["risk"]["level"], "high")
        finding = next(f for f in report["risk"]["findings"] if f["category"] == "crd_change")
        self.assertEqual(finding["evidence"]["path"], "charts/crds/crds/resource.yaml")
        self.assertIn("CRD diffs", compare.report_html(report))

    def test_required_template_values_retain_owning_template(self):
        self.write(self.new, "charts/child/templates/secret.yaml",
                   '{{ required "token" .Values.token }}\n')
        report = compare.compare_charts(self.old, self.new)
        finding = next(f for f in report["risk"]["findings"] if f["category"] == "new_template_requirements")
        self.assertEqual(finding["evidence"],
                         [{"template": "charts/child/templates/secret.yaml", "path": "/token"}])

    def test_root_json_pointer(self):
        self.assertEqual(compare.deep_diff(None, {"key": 1})[0]["path"], "")

    def test_html_highlights_negative_numbers_as_single_tokens(self):
        self.write(self.new, "values.yaml", "priority: -1\nthreshold: -1.5e+20\n")
        report = compare.compare_charts(self.old, self.new)
        rendered = compare.report_html(report)
        self.assertIn('<span class="literal">-1</span>', rendered)
        self.assertIn('<span class="literal">-1.5e+20</span>', rendered)

    def test_recursive_yaml_and_unsafe_tags_rejected(self):
        self.write(self.old, "values.yaml", "loop: &loop\n  child: *loop\n")
        with self.assertRaisesRegex(ValueError, "recursive"):
            compare.read_yaml(self.old / "values.yaml")
        self.write(self.old, "values.yaml", "!!python/object/apply:os.system ['echo unsafe']")
        with self.assertRaises(compare.yaml.YAMLError):
            compare.read_yaml(self.old / "values.yaml")


class ArchiveAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, members):
        path = self.root / "chart.tgz"
        with tarfile.open(path, "w:gz") as package:
            for name, value in members:
                info = tarfile.TarInfo(name)
                if value is None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/tmp"
                    package.addfile(info)
                else:
                    data = value.encode()
                    info.size = len(data)
                    package.addfile(info, io.BytesIO(data))
        return path

    def test_safe_extraction(self):
        archive = self.archive([("prometheus/Chart.yaml", "name: prometheus"),
                                ("prometheus/values.yaml", "{}")])
        root = compare.extract_chart(archive, self.root / "unpacked", "prometheus")
        self.assertEqual(root.name, "prometheus")
        self.assertEqual((root / "values.yaml").read_text(), "{}")

    def test_unsafe_members_are_rejected_before_writing_files(self):
        for index, (name, value) in enumerate([
            ("../escape", "bad"), ("/absolute", "bad"), ("prometheus/../../escape", "bad"),
            ("prometheus/link", None), ("other/Chart.yaml", "bad"), ("prometheus\\escape", "bad"),
        ]):
            with self.subTest(name=name):
                archive = self.archive([("prometheus/Chart.yaml", "name: prometheus"), (name, value)])
                destination = self.root / f"unpacked{index}"
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    compare.extract_chart(archive, destination, "prometheus")
                self.assertEqual(list(destination.iterdir()), [])

    def test_duplicate_and_oversized_archives(self):
        archive = self.archive([("prometheus/Chart.yaml", "{}"), ("prometheus/Chart.yaml", "{}")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            compare.extract_chart(archive, self.root / "duplicate", "prometheus")
        archive = self.archive([("prometheus/Chart.yaml", "large")])
        with patch.object(compare, "MAX_EXTRACTED_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "limits"):
                compare.extract_chart(archive, self.root / "large", "prometheus")

    def test_release_url_and_invalid_arguments(self):
        self.assertEqual(compare.release_url("prometheus", "prometheus-25.0.0"),
                         compare.RELEASE_BASE + "/prometheus-25.0.0/prometheus-25.0.0.tgz")
        for version in ("25.x.x", "../oops", "latest", "1.0.0/evil"):
            with self.assertRaises(ValueError):
                compare.release_url("prometheus", version)
        with self.assertRaises(ValueError):
            compare.release_url("../chart", "1.0.0")

    def test_download_limits_and_http_error(self):
        with patch.object(compare, "urlopen", return_value=io.BytesIO(b"abc")), \
                patch.object(compare, "MAX_ARCHIVE_BYTES", 2):
            with self.assertRaisesRegex(ValueError, "limit"):
                compare.download_chart("prometheus", "1.0.0", self.root / "large.tgz")
        error = HTTPError("https://github.com", 404, "Not Found", {}, None)
        with patch.object(compare, "urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "verify the release exists"):
                compare.download_chart("prometheus", "1.0.0", self.root / "missing.tgz")

    def test_cli_end_to_end_offline_and_repeated_runs(self):
        def download(chart, version, destination):
            with tarfile.open(destination, "w:gz") as package:
                for name, content in (("Chart.yaml", f"name: {chart}\nversion: {version}\n"),
                                      ("values.yaml", f"version: '{version}'\n")):
                    info = tarfile.TarInfo(f"{chart}/{name}")
                    data = content.encode()
                    info.size = len(data)
                    package.addfile(info, io.BytesIO(data))
            return {"url": compare.release_url(chart, version), "sha256": compare.file_info(destination)["sha256"]}

        with patch.object(compare, "download_chart", side_effect=download), contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.assertEqual(compare.main(["1.0.0", "2.0.0", "--output", str(self.root)]), 0)
        reports = list(self.root.glob("*/report.json"))
        self.assertEqual(len(reports), 2)
        report = json.loads(reports[0].read_text())
        self.assertIn("sha256", report["sources"]["old"])
        self.assertTrue((reports[0].parent / "old/prometheus/values.yaml").is_file())
        self.assertTrue((reports[0].parent / "new/prometheus/values.yaml").is_file())

    def test_cli_invalid_version_fails_without_creating_output(self):
        output = self.root / "output"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(compare.main(["25.x.x", "29.37.0", "--output", str(output)]), 1)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
