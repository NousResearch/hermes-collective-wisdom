"""Desktop handoff examples stay shell-safe and match the real CLI parser."""

import argparse
import ast
import re
import shlex
from pathlib import Path

from hermes_wisdom_plugin.commands import build_parser


def test_desktop_command_examples_are_shell_safe_and_parse():
    """Even unreplaced placeholders must not become shell redirections or pipelines."""
    source = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text()
    literals = re.findall(r'''command:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', source)
    assert literals, "the Desktop page must provide actual command handoffs"
    parser = argparse.ArgumentParser()
    build_parser(parser)
    for literal in literals:
        command = ast.literal_eval(literal)
        lexer = shlex.shlex(command, posix=True, punctuation_chars="<>|;&")
        lexer.whitespace_split = True
        tokens = list(lexer)
        assert not any(token in {"<", ">", "|", ";", "&", "&&", "||", ">>"}
                       for token in tokens), command
        assert tokens[:2] == ["hermes", "wisdom"], command
        # Substitute example values only for argparse's typed/enum parameters;
        # do not execute commands or alter receipt-bearing argument semantics.
        values = {"<version>": "1", "<index>": "0", "<prerequisite|setup|verify>": "setup"}
        parser.parse_args([values.get(token, token) for token in tokens[2:]])
