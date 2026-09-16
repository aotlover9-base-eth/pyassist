"""Unit tests for the command-line interface."""

from __future__ import annotations

from pyassist.cli import build_parser


def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.prompt is None
    assert args.model is None
    assert args.workspace is None
    assert args.auto_approve is False
    assert args.settings is None


def test_parser_custom_args() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "-p", "summarize file",
        "-m", "gpt-4o",
        "-w", "/tmp",
        "-y",
        "--set", "temperature=0.3",
        "--set", "stream=false",
    ])
    assert args.prompt == "summarize file"
    assert args.model == "gpt-4o"
    assert args.workspace == "/tmp"
    assert args.auto_approve is True
    assert args.settings == ["temperature=0.3", "stream=false"]
