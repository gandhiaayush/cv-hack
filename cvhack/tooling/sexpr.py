"""Minimal KiCad s-expression reader/writer.

Lists are Python lists, atoms are str, quoted strings are Q (a str subclass)
so they round-trip with quotes.
"""

from __future__ import annotations

from typing import Any


class Q(str):
    """A quoted string."""


def parse(text: str) -> Any:
    tokens = _tokenize(text)
    pos = 0

    def read() -> Any:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            out = []
            while tokens[pos] != ")":
                out.append(read())
            pos += 1
            return out
        return tok

    return read()


def _tokenize(text: str) -> list:
    out: list = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            out.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                    buf.append({"n": "\n", "t": "\t"}.get(text[j], text[j]))
                else:
                    buf.append(text[j])
                j += 1
            out.append(Q("".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            out.append(text[i:j])
            i = j
    return out


def dump(node: Any, indent: int = 0) -> str:
    if isinstance(node, list):
        if not node:
            return "()"
        simple = all(not isinstance(x, list) for x in node)
        if simple:
            return "(" + " ".join(_atom(x) for x in node) + ")"
        pad = "\t" * (indent + 1)
        parts = [_atom(node[0]) if not isinstance(node[0], list) else dump(node[0], indent + 1)]
        for x in node[1:]:
            parts.append("\n" + pad + dump(x, indent + 1) if isinstance(x, list) else " " + _atom(x))
        return "(" + "".join(parts) + "\n" + "\t" * indent + ")"
    return _atom(node)


def _atom(x: Any) -> str:
    if isinstance(x, Q):
        return '"' + x.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(x, float):
        return f"{x:.4f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))
    return str(x)


def find(node: list, key: str) -> list | None:
    for x in node:
        if isinstance(x, list) and x and x[0] == key:
            return x
    return None


def find_all(node: list, key: str) -> list[list]:
    return [x for x in node if isinstance(x, list) and x and x[0] == key]
