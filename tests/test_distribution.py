"""Static contracts for the intended public distribution surface.

These tests never start a server, watcher, or real Codex/Claude process, and
never touch the network. They only read files and parse text so that they
pass both in this development tree and in a clean copy that contains only
the Git-visible (non-ignored) public file set.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
README_PATH = ROOT / "README.md"
CHECKLIST_PATH = ROOT / "docs" / "RELEASE_CHECKLIST.md"
SELF_HOSTING_PATH = ROOT / "docs" / "SELF_HOSTING.md"
EXAMPLE_CONFIG = ROOT / "workflow.example.toml"
EXAMPLE_SERVICE = ROOT / "deploy" / "workflow-web.service.example"
LICENSE_PATH = ROOT / "LICENSE"
VENDOR_DIR = SKILL / "assets" / "web" / "vendor"

FORBIDDEN_PATH_FRAGMENT = "/home/ben"

# The owner-selected license identity for the first-party surface.
LICENSE_TITLE = "MIT License"
COPYRIGHT_LINE = "Copyright (c) 2026 Benjamin Crute"

# Assembled at runtime so this contract file can be scanned alongside every
# other first-party file without its own definition counting as a hit.
FORBIDDEN_IDENTITY = "Newf" + "Works"

# Words that would mean a license-mentioning line still frames licensing as
# something the owner has not settled.
UNSETTLED_MARKERS = (
    "unresolved",
    "undecided",
    "pending",
    "not yet",
    "still open",
    "still-open",
    "to be determined",
    "tbd",
)

# Binary or compiled artefacts that carry no reviewable prose.
NON_TEXT_SUFFIXES = frozenset({".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip"})

# Files whose text must never leak the developer's private home directory
# path into the intended public checkout.
NO_LEAK_FILES = [
    README_PATH,
    SELF_HOSTING_PATH,
    EXAMPLE_CONFIG,
    EXAMPLE_SERVICE,
    SKILL / "SKILL.md",
    CI_PATH,
    CHECKLIST_PATH,
]


def _gitignored_test_modules() -> set[str]:
    """Test basenames the accepted ignore rules keep out of the public checkout."""
    gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return {
        line.strip().lstrip("/").split("/", 1)[1]
        for line in gitignore_text.splitlines()
        if line.strip().startswith("/tests/") and line.strip().endswith(".py")
    }


def _public_test_modules() -> set[str]:
    """Test modules on disk that the public checkout would actually contain."""
    on_disk = {path.name for path in (ROOT / "tests").glob("test_*.py")}
    return on_disk - _gitignored_test_modules()


def _first_party_public_files() -> list[Path]:
    """Every intended-public file this repository authors itself.

    Deliberately excludes the vendored third-party directory: those files are
    verbatim upstream copies carrying upstream copyright notices, so they are
    not evidence about this project's own identity and must never be edited to
    match it.
    """
    paths = [
        README_PATH,
        LICENSE_PATH,
        CHECKLIST_PATH,
        SELF_HOSTING_PATH,
        EXAMPLE_CONFIG,
        EXAMPLE_SERVICE,
        CI_PATH,
        ROOT / ".gitignore",
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
    ]
    for path in sorted(SKILL.rglob("*")):
        if not path.is_file():
            continue
        if VENDOR_DIR in path.parents or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in NON_TEXT_SUFFIXES:
            continue
        paths.append(path)
    paths.extend(ROOT / "tests" / name for name in sorted(_public_test_modules()))
    return [path for path in paths if path.is_file()]


class RequiredFilesExistTests(unittest.TestCase):
    def test_required_public_files_exist(self) -> None:
        required = [
            README_PATH,
            LICENSE_PATH,
            ROOT / ".gitignore",
            ROOT / "AGENTS.md",
            ROOT / "CLAUDE.md",
            EXAMPLE_CONFIG,
            EXAMPLE_SERVICE,
            SELF_HOSTING_PATH,
            CHECKLIST_PATH,
            CI_PATH,
            SKILL / "SKILL.md",
            SKILL / "scripts" / "web_app.py",
        ]
        for path in required:
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing required file: {path}")

    def test_vendor_license_exists(self) -> None:
        vendor_license = SKILL / "assets" / "web" / "vendor" / "LICENSE.txt"
        self.assertTrue(vendor_license.is_file())
        text = vendor_license.read_text(encoding="utf-8")
        self.assertTrue(text.strip(), "vendor LICENSE.txt must not be empty")


class NoPrivatePathLeakTests(unittest.TestCase):
    def test_no_developer_home_path(self) -> None:
        for path in NO_LEAK_FILES:
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing file: {path}")
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    FORBIDDEN_PATH_FRAGMENT,
                    text,
                    f"{path} must not reference {FORBIDDEN_PATH_FRAGMENT}",
                )


class RootLicenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(LICENSE_PATH.is_file(), "repository must ship a root LICENSE")
        self.text = LICENSE_PATH.read_text(encoding="utf-8")

    def test_titled_mit(self) -> None:
        self.assertEqual(self.text.splitlines()[0].strip(), LICENSE_TITLE)

    def test_exact_personal_copyright_line(self) -> None:
        lines = [line.strip() for line in self.text.splitlines()]
        self.assertIn(COPYRIGHT_LINE, lines)
        copyright_lines = [line for line in lines if line.lower().startswith("copyright")]
        self.assertEqual(
            copyright_lines,
            [COPYRIGHT_LINE],
            "root LICENSE must carry exactly one copyright line, the owner's personal one",
        )

    def test_standard_mit_body(self) -> None:
        for clause in (
            "Permission is hereby granted, free of charge",
            "without restriction, including without limitation the rights",
            "The above copyright notice and this permission notice shall be included",
            'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
            "IN NO EVENT SHALL THE",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, self.text)


class ReadmeLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README_PATH.read_text(encoding="utf-8")

    def test_documented_relative_links_exist(self) -> None:
        links = re.findall(r"\]\(([^)]+)\)", self.text)
        for link in links:
            if link.startswith(("http://", "https://", "#")):
                continue
            with self.subTest(link=link):
                target = (ROOT / link).resolve()
                self.assertTrue(target.is_file(), f"README links to missing file: {link}")

    def test_documented_script_paths_exist(self) -> None:
        script_paths = set(re.findall(r"skills/coordinate-claude-work/scripts/\S+\.py", self.text))
        self.assertTrue(script_paths, "expected at least one documented script path")
        for rel in script_paths:
            with self.subTest(rel=rel):
                self.assertTrue((ROOT / rel).is_file(), f"documented script missing: {rel}")

    def test_mentions_ci_matrix_and_release_checklist(self) -> None:
        self.assertIn("3.11", self.text)
        self.assertIn("3.12", self.text)
        self.assertIn("3.13", self.text)
        self.assertIn("RELEASE_CHECKLIST.md", self.text)

    def test_declares_mit_license_under_personal_name(self) -> None:
        self.assertIn("## License", self.text)
        section = self.text.split("## License", 1)[1].split("\n## ", 1)[0]
        self.assertIn("MIT", section)
        self.assertIn("Benjamin Crute", section)
        self.assertIn("(LICENSE)", section)

    def test_still_framed_as_experimental_personal_project(self) -> None:
        self.assertIn("experimental personal", self.text.lower())


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_parses_to_loopback_safe_values(self) -> None:
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        # host is commented-out/default-documented, or explicitly loopback if present.
        host = data.get("host", "127.0.0.1")
        self.assertEqual(host, "127.0.0.1")
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("password", text.lower())
        self.assertNotIn("secret", text.lower())


class ExampleServiceUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = EXAMPLE_SERVICE.read_text(encoding="utf-8")

    def test_uses_placeholders_not_real_paths(self) -> None:
        self.assertIn("/path/to/", self.text)
        self.assertNotIn(FORBIDDEN_PATH_FRAGMENT, self.text)

    def test_documents_cli_path_setup(self) -> None:
        self.assertIn("PATH", self.text)
        self.assertIn("codex", self.text)
        self.assertIn("claude", self.text)

    def test_no_routable_bind_or_credentials(self) -> None:
        self.assertNotIn("0.0.0.0", self.text)
        for pattern in (r"password\s*=", r"secret\s*=", r"api_key\s*=", r"token\s*="):
            self.assertNotRegex(self.text.lower(), pattern)


class CiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CI_PATH.read_text(encoding="utf-8")

    def test_permissions_are_read_only(self) -> None:
        self.assertRegex(self.text, r"permissions:\s*\n\s*contents:\s*read")

    def test_python_version_matrix(self) -> None:
        for version in ("3.11", "3.12", "3.13"):
            with self.subTest(version=version):
                self.assertIn(f'"{version}"', self.text)

    def test_current_major_action_versions(self) -> None:
        self.assertIn("actions/checkout@v7", self.text)
        self.assertIn("actions/setup-python@v7", self.text)
        self.assertIn("actions/setup-node@v7", self.text)

    def test_setup_node_version_and_configuration(self) -> None:
        self.assertRegex(self.text, r'node-version:\s*"?24"?')
        self.assertRegex(self.text, r"package-manager-cache:\s*false")

    def test_no_install_cache_or_provider_agent_launch(self) -> None:
        forbidden_terms = [
            "pip install",
            "npm install",
            "npm ci",
            "actions/cache",
            "codex ",
            "claude ",
            "docker",
            "curl ",
            "wget ",
        ]
        lowered = self.text.lower()
        for term in forbidden_terms:
            with self.subTest(term=term):
                self.assertNotIn(term.lower(), lowered)
        # Forbid an actual cache: input/action, but allow the explicit
        # `package-manager-cache: false` safeguard used to make the
        # setup-node step deterministic without enabling any cache.
        self.assertNotRegex(self.text, r"(?<!package-manager-)cache:\s*(?!false)\S")

    def test_required_commands_present(self) -> None:
        self.assertIn("python -m compileall -q skills tests", self.text)
        self.assertIn("python -m unittest discover -s tests", self.text)
        self.assertIn("node --check", self.text)

    def test_runs_on_ubuntu_with_timeout(self) -> None:
        self.assertIn("ubuntu-latest", self.text)
        self.assertIn("timeout-minutes:", self.text)


class ReleaseChecklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CHECKLIST_PATH.read_text(encoding="utf-8")

    def test_covers_required_topics(self) -> None:
        required_terms = [
            "test",
            "credential",
            "vendor",
            "license",
            "smoke",
            "commit",
            "remote",
        ]
        lowered = self.text.lower()
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, lowered)

    def test_requires_verifying_the_root_mit_license(self) -> None:
        lowered = self.text.lower()
        self.assertIn("mit", lowered)
        self.assertIn("`license`", lowered)
        self.assertIn(COPYRIGHT_LINE.lower(), lowered)

    def test_retains_every_prior_publication_check(self) -> None:
        lowered = self.text.lower()
        for topic in (
            "unittest discover -s tests",
            "compileall -q skills tests",
            ".gitignore",
            "git status",
            "secret",
            "workflow.example.toml",
            "self_hosting.md",
            "vendor/license.txt",
            "smoke",
            "git diff --cached",
            "remote",
        ):
            with self.subTest(topic=topic):
                self.assertIn(topic, lowered)

    def test_does_not_claim_public_push_happened(self) -> None:
        lowered = self.text.lower()
        self.assertNotIn("has been pushed", lowered)
        self.assertNotIn("already published", lowered)


class PublicCheckoutUnitDiscoveryTests(unittest.TestCase):
    """Confirm the currently-tracked (non-ignored) test files match the
    intended public checkout's workflow/web/session/distribution scope.

    This inspects .gitignore's explicit per-file test exclusions rather than
    running `git`, so it stays valid both in this dev tree and in a clean
    copy built from the accepted ignore rules (where the excluded files are
    simply absent).
    """

    EXPECTED_PUBLIC_TEST_MODULES = frozenset(
        {
            "test_workflow.py",
            "test_web_settings.py",
            "test_web_workflow_state.py",
            "test_web_terminal_contract.py",
            "test_web_views.py",
            "test_web_repository_picker.py",
            "test_web_repository_switching.py",
            "test_codex_session.py",
            "test_codex_session_http.py",
            "test_distribution.py",
        }
    )

    def test_local_demo_and_unrelated_tests_are_excluded_or_absent(self) -> None:
        excluded_basenames = [
            "test_cli.py",
            "test_examples.py",
            "test_markdown.py",
            "test_readiness_checklist.py",
            "test_work_item.py",
        ]
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        tests_dir = ROOT / "tests"
        for name in excluded_basenames:
            with self.subTest(name=name):
                present_on_disk = (tests_dir / name).is_file()
                ignored = f"/tests/{name}" in gitignore_text
                # Either this dev tree still has the file locally (and it is
                # ignored so it won't reach the public checkout), or a clean
                # copy simply never had the file to begin with.
                self.assertTrue(
                    ignored or not present_on_disk,
                    f"{name} must be gitignored or absent from the public checkout",
                )

    def test_public_test_set_equals_expected_after_gitignore_exclusions(self) -> None:
        """Derive the public test module set as (all test_*.py on disk) minus
        (the exact per-file .gitignore exclusions), and assert it equals the
        expected ten modules exactly, so an accidental extra public test
        file fails this contract rather than passing silently.
        """
        self.assertEqual(_public_test_modules(), self.EXPECTED_PUBLIC_TEST_MODULES)


class FirstPartyLicenseIdentityTests(unittest.TestCase):
    """The first-party surface must speak with one settled license identity.

    Vendored third-party notices are excluded on purpose: they are verbatim
    upstream copies whose own copyright lines are correct as they stand.
    """

    def setUp(self) -> None:
        self.paths = _first_party_public_files()
        self.assertGreater(len(self.paths), 10, "first-party file scan looks empty")

    def test_vendor_notices_are_excluded_from_the_scan(self) -> None:
        vendor_license = VENDOR_DIR / "LICENSE.txt"
        self.assertTrue(vendor_license.is_file())
        self.assertNotIn(vendor_license, self.paths)
        for path in self.paths:
            with self.subTest(path=path):
                self.assertNotIn(VENDOR_DIR, path.parents)

    def test_no_rejected_organization_name(self) -> None:
        for path in self.paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(
                    FORBIDDEN_IDENTITY,
                    text,
                    f"{path} names an organization the owner did not select",
                )

    def test_license_mentions_are_settled(self) -> None:
        for path in self.paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                lowered = line.lower()
                if "licen" not in lowered:
                    continue
                for marker in UNSETTLED_MARKERS:
                    with self.subTest(path=path, line=number, marker=marker):
                        self.assertNotIn(
                            marker,
                            lowered,
                            f"{path}:{number} still frames licensing as an open question: {line.strip()!r}",
                        )


if __name__ == "__main__":
    unittest.main()
