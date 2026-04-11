"""Unit tests for patch_acceptance_math (no Core API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import patch_acceptance_math as pam  # noqa: E402


def test_safe_eval_arith_basic() -> None:
    assert pam.safe_eval_arith(" (1 + 2) * 4 ") == 12.0
    assert pam.safe_eval_arith("3*(2+3)") == 15.0


def test_safe_eval_arith_rejects_call() -> None:
    with pytest.raises(ValueError, match="disallowed"):
        pam.safe_eval_arith("__import__('os').system('x')")


def test_normalize_expr() -> None:
    assert pam.normalize_expr(" 3 * ( 2 + 1 ) ") == "3*(2+1)"


def test_extract_json_object() -> None:
    text = 'Here you go:\n{"post_id": 42, "expr": "(1+1)", "result": 2}\nThanks'
    obj = pam.extract_json_object(text)
    assert obj == {"post_id": 42, "expr": "(1+1)", "result": 2}


def test_grade_case_a_pass() -> None:
    r = pam.grade_case_a(
        reply_text='{"post_id": 7, "expr": "(1+2)*4", "result": 12}',
        expected_post_id=7,
        expected_expr="(1+2)*4",
        stale_expr="(1+2)*3",
    )
    assert r["pass"] is True


def test_grade_case_a_stale() -> None:
    r = pam.grade_case_a(
        reply_text='{"post_id": 7, "expr": "(1+2)*3", "result": 9}',
        expected_post_id=7,
        expected_expr="(1+2)*4",
        stale_expr="(1+2)*3",
    )
    assert r["pass"] is False
    assert r["error"] == "used_stale_expr"


def test_grade_case_b_pass() -> None:
    r = pam.grade_case_b(
        reply_text='{"post_ids": [10, 20, 30], "combined_expr": "3*(2+3)", "result": 15}',
        expected_post_ids=[10, 20, 30],
        expected_combined_expr="3*(2+3)",
    )
    assert r["pass"] is True
