"""Architectural fitness function.

The first AST statement of execute_booking() in booking/two_phase.py must be
a verify_token(...) call. Any future engineer who removes or reorders this
check fails CI.
"""

from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "booking" / "two_phase.py"


def test_execute_booking_first_statement_is_verify_token():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    func = next(
        (n for n in tree.body
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_booking"),
        None,
    )
    assert func is not None, "execute_booking not found in booking/two_phase.py"

    # Find the first executable statement (skip docstrings)
    body = list(func.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]

    assert body, "execute_booking has empty body"
    first = body[0]

    # Either: payload = verify_token(...)  OR  await verify_token(...)
    # We accept the assignment form used in two_phase.py.
    assert isinstance(first, ast.Assign), (
        f"First statement of execute_booking must be a verify_token assignment; "
        f"got {type(first).__name__}"
    )
    call = first.value
    assert isinstance(call, ast.Call), (
        "First statement value must be a Call to verify_token"
    )
    func_name = call.func.id if isinstance(call.func, ast.Name) else (
        call.func.attr if isinstance(call.func, ast.Attribute) else None
    )
    assert func_name == "verify_token", (
        f"First call must be verify_token; got {func_name}"
    )


def test_no_provider_sdk_imports_outside_booking():
    """No file outside booking/providers/ imports stripe / amadeus directly.

    All provider integrations must go through the PaymentProvider Protocol or
    the corresponding tool adapter. This keeps the trust gate enforcement at
    the seams.
    """
    project_root = Path(__file__).resolve().parents[2]
    forbidden = ("import stripe", "from stripe", "import amadeus", "from amadeus")
    offenders = []
    for py in project_root.rglob("*.py"):
        rel = py.relative_to(project_root).as_posix()
        if (rel.startswith("booking/providers/")
                or rel.startswith("tools/")
                or rel.startswith("tests/")):
            continue
        text = py.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert not offenders, (
        "Provider SDK imports outside the adapters layer:\n  " + "\n  ".join(offenders)
    )
