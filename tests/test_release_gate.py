"""Keep the 1.0 gate document tied to the suites and CI jobs that actually exist.

The gate document is prose, but two of its claims are mechanical: the suite modes it names must be modes the
Linux runner accepts, and the CI jobs it names must be jobs the test workflow defines. The gate index in the
document is the machine-readable list; the runner and the workflow are parsed, never copied.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "docs" / "release-gate.md"
RUNNER = ROOT / "tests" / "linux" / "run.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"

INDEX = re.compile(r"<!--\s*gate-index\s*\n(?P<body>.*?)\n-->", re.S)
STATUS_ROW = re.compile(
    r"^\|\s*([1-7])\s*\|.*?\|\s*(Verified|Partly verified|Not verified)\s*\|", re.M)
ALLOWED_STATUSES = {"Verified", "Partly verified", "Not verified"}


def gate_text() -> str:
    return GATE.read_text(encoding="utf-8")


def index_values(name: str) -> list[str]:
    match = INDEX.search(gate_text())
    if match is None:
        raise AssertionError("docs/release-gate.md has no gate-index block")
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == name:
            return value.split()
    raise AssertionError(f"the gate index has no {name} line")


def suite_modes() -> set[str]:
    """Every mode the runner accepts, read from its own case statement."""
    text = RUNNER.read_text(encoding="utf-8")
    match = re.search(r'case "\$mode" in\n(?P<body>.*?)\nesac', text, re.S)
    if match is None:
        raise AssertionError("tests/linux/run.sh has no mode case statement")
    modes = set()
    for line in match.group("body").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        arm, separator, _command = line.partition(")")
        if not separator:
            continue
        for mode in arm.split("|"):
            mode = mode.strip()
            if re.fullmatch(r"[a-z0-9]+", mode):
                modes.add(mode)
    return modes


def ci_jobs() -> dict[str, str]:
    """Every job under `jobs:` in the test workflow, with its body, parsed by indentation."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    jobs: dict[str, str] = {}
    in_jobs = False
    current: str | None = None
    for line in lines:
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match:
            current = match.group(1)
            jobs[current] = ""
        elif current is not None:
            jobs[current] += line + "\n"
    if not jobs:
        raise AssertionError(".github/workflows/test.yml has no jobs")
    return jobs


def real_host_jobs() -> set[str]:
    """Jobs that run a Linux suite, either through the runner or through a real-host environment flag."""
    return {name for name, body in ci_jobs().items()
            if "tests/linux/run.sh" in body or "SHIPLORD_TEST_" in body}


class ReleaseGateTests(unittest.TestCase):
    def test_the_gate_names_only_suite_modes_the_runner_accepts(self):
        declared = set(index_values("suites"))
        self.assertTrue(declared, "the gate index names no suite modes")
        self.assertEqual(declared - suite_modes(), set(),
                         "docs/release-gate.md names a suite mode tests/linux/run.sh does not accept")

    def test_the_gate_names_every_suite_mode_the_runner_accepts(self):
        self.assertEqual(suite_modes() - set(index_values("suites")), set(),
                         "tests/linux/run.sh has a mode docs/release-gate.md does not name")

    def test_the_gate_names_only_ci_jobs_the_workflow_defines(self):
        declared = set(index_values("ci_jobs"))
        self.assertTrue(declared, "the gate index names no CI jobs")
        self.assertEqual(declared - set(ci_jobs()), set(),
                         "docs/release-gate.md names a CI job .github/workflows/test.yml does not define")

    def test_the_gate_names_every_ci_job_that_runs_a_real_host_suite(self):
        self.assertEqual(real_host_jobs() - set(index_values("ci_jobs")), set(),
                         "a real-host CI job is missing from the gate index")

    def test_the_gate_has_seven_criteria_with_an_allowed_status(self):
        rows = STATUS_ROW.findall(gate_text())
        self.assertEqual([number for number, _status in rows], [str(number) for number in range(1, 8)],
                         "the gate table must have exactly the seven criteria, once each")
        self.assertTrue(all(status in ALLOWED_STATUSES for _number, status in rows),
                        "a gate criterion has a status outside Verified, Partly verified, Not verified")


if __name__ == "__main__":
    unittest.main()
