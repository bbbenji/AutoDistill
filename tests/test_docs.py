"""Every documented command must still be a command.

A CI job once failed because the workflow invoked `--openpilot`, a flag that had
been replaced by `--port` several commits earlier. Nothing caught it: the flag
lived in a YAML file and a shell script, neither of which anything checked, and
the failure surfaced only after a push.

This walks the README, the example scripts and the CI workflow, pulls out every
`autodistill-can ...` invocation, and hands it to the real argument parser. It runs
nothing — parsing is enough to prove the flags exist.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from autodistill_can.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent

#: Files that contain runnable examples people will copy.
_DOC_SOURCES = [
    ROOT / "README.md",
    ROOT / "README_SIMPLE.md",
    *sorted((ROOT / "examples").glob("*.sh")),
    *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
]

# An invocation starts a line, optionally behind a `$ ` prompt or a `time`.
# That is what separates one from an inline mention in a sentence, which is
# usually just the command name and would look like a call with no arguments.
#
# The `\\\n` alternative comes first so a shell line continuation is consumed
# as one unit rather than ending the match at the newline. A backtick ends
# the match so that prose following an inline-code example is not swallowed.
_INVOCATION = re.compile(
    r"(?m)^[\s`]*(?:\$\s*)?(?:time\s+)?autodistill-can\s+"
    r"((?:\\\n|[^\n|&;<>#`])+)"
)


def _documented_commands() -> list[tuple[str, str]]:
    """(source file, argument string) for each documented invocation."""
    found: list[tuple[str, str]] = []
    for path in _DOC_SOURCES:
        if not path.exists():
            continue
        text = path.read_text()
        for match in _INVOCATION.finditer(text):
            args = re.sub(r"\\\n\s*", " ", match.group(1)).strip()
            # Skip prose mentions ("autodistill-can probe" in a sentence) and the
            # console-script definition in packaging metadata.
            if not args or args.startswith(("=", ":")):
                continue
            first = args.split()[0]
            if first not in {a for a in _SUBCOMMANDS}:
                continue
            found.append((path.name, args))
    return found


def _subcommands() -> set[str]:
    """Every subcommand the parser accepts, taken from the parser.

    Hardcoding this list meant a newly added command was documented but never
    checked -- the one situation these tests exist to prevent.
    """
    for action in build_parser()._subparsers._group_actions:
        if getattr(action, "choices", None):
            return set(action.choices)
    raise AssertionError("no subparsers found")


_SUBCOMMANDS = _subcommands()


def test_documentation_contains_examples():
    assert _documented_commands(), "no documented commands found to check"


@pytest.mark.parametrize(
    "source,args",
    _documented_commands(),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_documented_command_parses(source, args):
    parser = build_parser()
    try:
        parser.parse_args(shlex.split(args))
    except SystemExit as exc:  # argparse exits on an unknown flag
        pytest.fail(
            f"{source}: `autodistill-can {args}` is no longer valid ({exc})"
        )


def test_the_ci_fixture_generator_matches_the_pipeline():
    """`tools/control_facts.py` must analyse the way the CLI does.

    It builds the facts file CI generates a control port from, via
    `autodistill_can.demo` (also used by the web UI's demo project). Analysing
    per message instead of through `analyse_log` skips the whole-car byte-order
    decision, which can split payloads differently and name signals
    differently -- yielding a facts file `port` then rejects because the fields
    it references do not exist. CI only passed because the capture length
    happened to make both paths agree.
    """
    tool_source = (ROOT / "tools" / "control_facts.py").read_text()
    assert "analyse_for_demo_facts" in tool_source
    demo_source = (ROOT / "autodistill_can" / "demo.py").read_text()
    assert "detect_byte_order" in demo_source
    assert "analyse_log(" in demo_source
    assert "analyse_message(" not in demo_source


def test_the_package_and_project_versions_agree():
    """The version is declared twice, so it can be wrong in one place.

    `pyproject.toml` feeds the wheel; `__version__` feeds `--version` and the
    web UI. A user reading a version off one and reporting a bug against the
    other is a bad afternoon for everybody.
    """
    import re as _re

    from autodistill_can import __version__

    pyproject = (ROOT / "pyproject.toml").read_text()
    declared = _re.search(r'^version = "([^"]+)"', pyproject, _re.M)
    assert declared, "pyproject.toml has no version"
    assert declared.group(1) == __version__


def test_the_changelog_documents_the_current_version():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    from autodistill_can import __version__

    assert f"## {__version__}" in changelog, (
        f"CHANGELOG.md has no entry for {__version__}"
    )
