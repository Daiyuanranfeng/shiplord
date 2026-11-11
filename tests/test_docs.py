from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

from typer.testing import CliRunner

from shiplord import cli

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide.md"
NATIVE_SECTION = "### Releasing a native binary"


def guide_section(text: str, heading: str) -> str:
    """The lines under ``heading``, stopping at the next heading outside a code fence."""
    lines = text.splitlines()
    body: list[str] = []
    fenced = False
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            break
        body.append(line)
    return "\n".join(body)


def shiplord_invocations(text: str) -> list[list[str]]:
    """Every shell line in a ``sh`` block that invokes the CLI, split into argv tokens."""
    invocations: list[list[str]] = []
    block = None
    for line in text.splitlines():
        if line.startswith("```"):
            block = None if block is not None else line[3:].strip()
        elif block == "sh" and line.strip().startswith("shiplord "):
            invocations.append(shlex.split(line.strip()))
    return invocations


class GuideCommandTests(unittest.TestCase):
    def test_native_release_commands_parse_against_the_real_cli(self):
        invocations = shiplord_invocations(guide_section(GUIDE.read_text(), NATIVE_SECTION))
        self.assertGreaterEqual(len(invocations), 3, invocations)
        options = {token for invocation in invocations for token in invocation if token.startswith("--")}
        self.assertTrue({"--binary", "--commit", "--sequence"} <= options, options)
        for invocation in invocations:
            with self.subTest(command=" ".join(invocation)):
                # --help parses every option but stops before the command body runs.
                result = CliRunner().invoke(cli.app, invocation[1:] + ["--help"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertNotIn("No such option", result.output)
