"""Вспомогательные функции для приёмочных тестов приоритета context_patch (арифметика).

Безопасное вычисление только для выражений из цифр, скобок и +-*/ — через ast.
"""
from __future__ import annotations

import ast
import json
import operator
import re
from typing import Any

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def normalize_expr(s: str) -> str:
    """Убираем пробелы для сравнения строк выражений."""
    return re.sub(r"\s+", "", (s or "").strip())


def safe_eval_arith(expr: str) -> float:
    """Вычислить арифметическое выражение (int/float, + - * /, скобки)."""
    raw = (expr or "").strip()
    if not raw:
        raise ValueError("empty expression")
    tree = ast.parse(raw, mode="eval")

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                raise ValueError("bool not allowed")
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise ValueError("disallowed constant type")
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return float(_UNARY[type(node.op)](_eval(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return float(_BINOPS[type(node.op)](_eval(node.left), _eval(node.right)))
        raise ValueError(f"disallowed syntax: {type(node).__name__}")

    return _eval(tree)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Вытащить первый JSON-объект из ответа модели (возможен текст вокруг)."""
    if not text or not text.strip():
        return None
    t = text.strip()
    # Сначала весь текст как JSON
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                chunk = t[start : i + 1]
                try:
                    v = json.loads(chunk)
                    return v if isinstance(v, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def grade_case_a(
    *,
    reply_text: str,
    expected_post_id: int,
    expected_expr: str,
    stale_expr: str | None = None,
) -> dict[str, Any]:
    """Оценка сценария «один редактируемый пост».

    Ожидаемый JSON: {\"post_id\": int, \"expr\": str, \"result\": number}
    """
    expected_norm = normalize_expr(expected_expr)
    stale_norm = normalize_expr(stale_expr) if stale_expr else None
    obj = extract_json_object(reply_text)
    if not obj:
        return {"pass": False, "error": "no_json_object", "detail": reply_text[:500]}

    pid = obj.get("post_id")
    expr = obj.get("expr")
    result = obj.get("result")

    if not isinstance(pid, int) and not (isinstance(pid, float) and pid == int(pid)):
        return {"pass": False, "error": "bad_post_id", "obj": obj}
    pid_i = int(pid)
    if pid_i != expected_post_id:
        return {"pass": False, "error": "wrong_post_id", "expected": expected_post_id, "got": pid_i}

    if not isinstance(expr, str):
        return {"pass": False, "error": "expr_not_string", "obj": obj}

    got_norm = normalize_expr(expr)
    if stale_norm and got_norm == stale_norm:
        return {
            "pass": False,
            "error": "used_stale_expr",
            "expected_norm": expected_norm,
            "got_norm": got_norm,
        }
    if got_norm != expected_norm:
        return {
            "pass": False,
            "error": "expr_mismatch",
            "expected_norm": expected_norm,
            "got_norm": got_norm,
        }

    try:
        computed = safe_eval_arith(expr)
    except Exception as e:  # noqa: BLE001
        return {"pass": False, "error": "eval_failed", "exception": str(e), "expr": expr}

    if not isinstance(result, (int, float)):
        return {"pass": False, "error": "result_not_number", "obj": obj}

    if abs(float(result) - computed) > 1e-9:
        return {
            "pass": False,
            "error": "result_inconsistent",
            "claimed": result,
            "computed": computed,
        }

    if abs(computed - safe_eval_arith(expected_expr)) > 1e-9:
        return {"pass": False, "error": "internal_expected_mismatch"}

    return {"pass": True, "post_id": pid_i, "expr_norm": got_norm, "result": float(result)}


def grade_case_b(
    *,
    reply_text: str,
    expected_post_ids: list[int],
    expected_combined_expr: str,
) -> dict[str, Any]:
    """Оценка сценария «несколько постов с SEG:».

    Ожидаемый JSON:
    {\"post_ids\": [int,...], \"combined_expr\": str, \"result\": number}
    """
    expected_norm = normalize_expr(expected_combined_expr)
    exp_ids = sorted(expected_post_ids)
    obj = extract_json_object(reply_text)
    if not obj:
        return {"pass": False, "error": "no_json_object", "detail": reply_text[:500]}

    pids = obj.get("post_ids")
    cexpr = obj.get("combined_expr")
    result = obj.get("result")

    if not isinstance(pids, list):
        return {"pass": False, "error": "post_ids_not_list", "obj": obj}
    try:
        got_ids = sorted(int(x) for x in pids)
    except (TypeError, ValueError):
        return {"pass": False, "error": "post_ids_not_ints", "obj": obj}

    if got_ids != exp_ids:
        return {"pass": False, "error": "post_ids_mismatch", "expected": exp_ids, "got": got_ids}

    if not isinstance(cexpr, str):
        return {"pass": False, "error": "combined_expr_not_string", "obj": obj}

    if normalize_expr(cexpr) != expected_norm:
        return {
            "pass": False,
            "error": "combined_expr_mismatch",
            "expected_norm": expected_norm,
            "got_norm": normalize_expr(cexpr),
        }

    try:
        computed = safe_eval_arith(cexpr)
    except Exception as e:  # noqa: BLE001
        return {"pass": False, "error": "eval_failed", "exception": str(e), "cexpr": cexpr}

    if not isinstance(result, (int, float)):
        return {"pass": False, "error": "result_not_number", "obj": obj}

    if abs(float(result) - computed) > 1e-9:
        return {
            "pass": False,
            "error": "result_inconsistent",
            "claimed": result,
            "computed": computed,
        }

    return {"pass": True, "combined_norm": expected_norm, "result": float(result)}
