"""Minimal S-expression reader for KiCad files (standard library only)."""

from __future__ import annotations

import re

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text: str):
    """Parse S-expression text into nested lists. Strings lose their quotes."""
    stack: list[list] = [[]]
    for match in _TOKEN.finditer(text):
        tok = match.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in S-expression")
            node = stack.pop()
            stack[-1].append(node)
        elif tok.startswith('"'):
            stack[-1].append(bytes(tok[1:-1], "utf-8").decode("unicode_escape")
                             if "\\" in tok else tok[1:-1])
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in S-expression")
    return stack[0]


def children(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def value(node, name, default=None):
    c = child(node, name)
    return c[1] if c is not None and len(c) > 1 else default


def is_number(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def xy(node) -> tuple[float, float]:
    return float(node[1]), float(node[2])
