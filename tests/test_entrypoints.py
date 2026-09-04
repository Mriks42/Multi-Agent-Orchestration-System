"""Every module must at least import, and every command must at least parse args.

A syntax error in an entry point once survived a full green test run and only
surfaced when a ten-minute eval failed instantly: nothing in the suite imported
the CLI modules, because they are the one layer no unit test touches.

These tests are shallow on purpose. They do not check behaviour -- they check
that the code is loadable and the argument parsers are wired, which is the class
of breakage that otherwise reaches a real run.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import mas

ENTRY_POINTS = [
    ("mas.cli", "mas"),
    ("mas.distributed.cli", "mas-worker"),
    ("mas.evals.cli", "mas-eval"),
    ("mas.evals.label_cli", "mas-label"),
    ("mas.evals.ablate_cli", "mas-ablate"),
    ("mas.web.cli", "mas-serve"),
]


def _all_modules() -> list[str]:
    return [
        name
        for _, name, _ in pkgutil.walk_packages(mas.__path__, prefix="mas.")
    ]


@pytest.mark.parametrize("module", _all_modules())
def test_every_module_imports(module):
    """Catches syntax errors and broken imports anywhere in the package."""
    importlib.import_module(module)


@pytest.mark.parametrize("module,command", ENTRY_POINTS)
def test_every_command_builds_its_parser(module, command):
    parser = importlib.import_module(module).build_parser()
    assert parser.prog == command


@pytest.mark.parametrize("module,command", ENTRY_POINTS)
def test_every_command_prints_help_without_touching_the_network(module, command, capsys):
    """--help must work with no API key and no side effects."""
    with pytest.raises(SystemExit) as exit_info:
        importlib.import_module(module).main(["--help"])

    assert exit_info.value.code == 0
    assert command in capsys.readouterr().out


def test_the_report_command_requires_company_and_quarter():
    """Both are required; neither has a usable default."""
    from mas.cli import build_parser

    for argv in ([], ["--company", "NVIDIA"], ["--quarter", "Q1 2025"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)

    args = build_parser().parse_args(["--company", "NVIDIA", "--quarter", "Q1 2025"])
    assert args.company == "NVIDIA" and args.quarter == "Q1 2025"
