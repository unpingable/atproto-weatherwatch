#!/usr/bin/env python3
"""Reject f-strings that only parse on Python 3.12+.

`pyproject.toml` says `requires-python = ">=3.10"`, CI runs a 3.10 leg, and
**the serving host is 3.10**. This workstation is 3.12, where PEP 701 lifted
three restrictions that still apply everywhere this code actually runs:

    f"{d["k"]}"            same quote reused inside the expression
    f"{x if y else "\n"}"  a backslash anywhere in the expression

(PEP 701 also allowed a newline *inside a single expression*. That one is not
checked here: distinguishing it from the triple-quoted f-string spanning many
lines that this codebase uses everywhere needs brace-depth tracking, and a
check that cries wolf 1,600 times is a check nobody runs. The two rules below
are token-local, have no false positives, and cover the failure that actually
shipped.)

`ast.parse(..., feature_version=(3, 10))` does **not** catch these: the
feature_version switch governs a short list of grammar features and does not
downgrade the tokenizer, so a 3.12 parser accepts all three silently. The only
local check that works is to walk the token stream ourselves.

This exists because the campaign that added it shipped exactly this bug and
found out from CI. It is the second time the gap between the development
interpreter and the deployed one has produced a red build in this repository;
the first was `timeutil` and a trailing `Z`.

Exit 0 clean, 1 with findings.
"""

from __future__ import annotations

import io
import pathlib
import sys
import token as T
import tokenize

ROOTS = ("src", "tests", "spike")

#: Longest first, so a triple delimiter is never mistaken for a single one.
DELIMS = ('"""', "'''", '"', "'")


def _delim(literal: str) -> str:
    """The quote delimiter of a string token, prefixes stripped."""
    body = literal.lstrip("rbuRBUfF")
    for d in DELIMS:
        if body.startswith(d):
            return d
    return ""


def offences(path: pathlib.Path) -> list[str]:
    """Pre-3.12 f-string violations in one file."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: unreadable ({exc})"]

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        return [f"{path}: could not tokenise ({exc})"]

    # Python 3.11 and earlier emit an f-string as a single STRING token, so
    # there is nothing to walk and nothing to check -- and nothing to get
    # wrong, because that tokenizer would have rejected the file already.
    start = getattr(T, "FSTRING_START", None)
    if start is None:
        return []
    middle, end = T.FSTRING_MIDDLE, T.FSTRING_END

    out: list[str] = []
    stack: list[str] = []                  # enclosing quote characters
    for tok in toks:
        if tok.type == start:
            stack.append(_delim(tok.string))
            continue
        if tok.type == end:
            if stack:
                stack.pop()
            continue
        if not stack:
            continue
        # FSTRING_MIDDLE is the literal text between expressions; its escapes
        # are fine on every version. Everything else here is expression text.
        if tok.type == middle:
            continue
        quote = stack[-1]
        where = f"{path}:{tok.start[0]}"
        if "\\" in tok.string:
            out.append(f"{where}: backslash inside an f-string expression "
                       f"({tok.string.strip()!r}) — SyntaxError before 3.12")
        elif tok.type == T.STRING and _delim(tok.string) == quote:
            # The EXACT delimiter, not merely the same character. A single
            # quote inside a triple-quoted f-string is legal on every version,
            # and reporting those buries the real findings under hundreds of
            # false ones — which is how a check stops being run.
            out.append(f"{where}: f-string expression reuses the enclosing "
                       f"delimiter {quote!r} — SyntaxError before 3.12")
    return out


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    found: list[str] = []
    checked = 0
    for name in ROOTS:
        base = root / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            checked += 1
            found.extend(offences(path))
    if found:
        print(f"!! {len(found)} f-string(s) that Python 3.10 cannot parse:",
              file=sys.stderr)
        for line in found:
            print(f"   {line}", file=sys.stderr)
        return 1
    print(f"OK: {checked} files carry no post-3.11 f-string syntax.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
