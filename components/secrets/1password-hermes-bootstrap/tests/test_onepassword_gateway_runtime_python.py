import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest import mock


COMPONENT = Path(__file__).resolve().parents[1]
SCRIPT = COMPONENT / "scripts" / "install_onepassword_hermes_candidate.sh"
OFFICIAL_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"
SECURITY_LOOKUP = (
    'token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)'
)
LIVE_BOOTSTRAP_ROOT = (
    'bootstrap_root="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap"'
)


def replace_source_once(source: str, expected: str, replacement: str) -> str:
    occurrences = source.count(expected)
    if occurrences != 1:
        raise AssertionError(
            f"expected exactly one source occurrence, found {occurrences}: {expected!r}"
        )
    return source.replace(expected, replacement, 1)


def build_harness(source: str, bootstrap_root: Path) -> str:
    harness_source = replace_source_once(source, SECURITY_LOOKUP, 'token="fake-token"')
    harness_source = replace_source_once(
        harness_source,
        LIVE_BOOTSTRAP_ROOT,
        f'bootstrap_root="{bootstrap_root}"',
    )
    if "/usr/bin/security" in harness_source:
        raise AssertionError("live Keychain executable remains in harness")
    if "/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap" in harness_source:
        raise AssertionError("live bootstrap path remains in harness")
    return harness_source


class CandidateInstallerContractTests(unittest.TestCase):
    def test_candidate_installer_defaults_to_signed_psf_python(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(OFFICIAL_PYTHON, source)
        self.assertNotIn("/opt/homebrew/opt/python@3.13", source)

    def test_candidate_installer_references_existing_canonical_sources(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"$ROOT/requirements/onepassword-hermes-gateway-sdk.txt"', source)
        self.assertTrue(
            (COMPONENT / "requirements" / "onepassword-hermes-gateway-sdk.txt").is_file()
        )
        self.assertIn('"$ROOT/scripts/onepassword_hermes_gateway_sdk_bootstrap.py"', source)
        self.assertTrue(
            (COMPONENT / "scripts" / "onepassword_hermes_gateway_sdk_bootstrap.py").is_file()
        )

    def test_gateway_wrapper_selects_candidate_only_for_general(self) -> None:
        source_path = COMPONENT.parents[2] / "scripts" / "hermes-gateway-keychain.sh"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn('hermes_executable="/Users/mutlupolatcan/.local/bin/hermes"', source)
        self.assertIn('if [[ "$profile" == "general" ]]; then', source)
        self.assertIn(
            'hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/'
            'hermes-agent-8488dc3717b5b22ed1923b108dd6bd54c47adc30/venv/bin/hermes"',
            source,
        )
        self.assertNotIn("$2", source)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "argv"
            bootstrap_root = root / "bootstrap"
            fake_python = bootstrap_root / "venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n", encoding="utf-8"
            )
            fake_python.chmod(0o700)
            (bootstrap_root / "hermes_gateway_sdk_bootstrap.py").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            (bootstrap_root / "hermes_gateway_sdk_bootstrap.py").chmod(0o700)

            harness = root / "wrapper"
            harness_source = build_harness(source, bootstrap_root)
            harness.write_text(harness_source, encoding="utf-8")
            harness.chmod(0o700)

            candidate = (
                "/Users/mutlupolatcan/.hermes/runtime/releases/"
                "hermes-agent-8488dc3717b5b22ed1923b108dd6bd54c47adc30/venv/bin/hermes"
            )
            for profile, expected in (
                [("general", candidate)]
                + [
                    (profile, "/Users/mutlupolatcan/.local/bin/hermes")
                    for profile in (
                        "assistant",
                        "researcher",
                        "coder",
                        "writer",
                        "producer",
                        "marketing",
                        "health",
                        "finance",
                    )
                ]
            ):
                result = subprocess.run(
                    ["/bin/zsh", str(harness), profile],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={"CAPTURE": str(capture)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    capture.read_text(encoding="utf-8").splitlines(),
                    [
                        str(bootstrap_root / "hermes_gateway_sdk_bootstrap.py"),
                        profile,
                        "--hermes-executable",
                        expected,
                    ],
                )

    def test_harness_source_drift_fails_before_subprocess(self) -> None:
        source_path = COMPONENT.parents[2] / "scripts" / "hermes-gateway-keychain.sh"
        source = source_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_root = Path(tmp) / "bootstrap"
            drifted_sources = (
                source.replace(SECURITY_LOOKUP, "", 1),
                source + "\n" + SECURITY_LOOKUP,
                source.replace(SECURITY_LOOKUP, SECURITY_LOOKUP.replace("token=", "token ="), 1),
            )
            with mock.patch.object(subprocess, "run") as run:
                for drifted in drifted_sources:
                    with self.assertRaises(AssertionError):
                        build_harness(drifted, bootstrap_root)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
