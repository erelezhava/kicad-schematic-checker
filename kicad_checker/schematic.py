"""Read KiCad schematic files directly (S-expressions) and find the active design.

Shared by `extract` (which files define the design) and tools/fill_fields.py.
Positions are kept so a caller can splice edits without reformatting the file.
"""

import re
from pathlib import Path


# --- minimal S-expression scanner that keeps exact text positions ---------------------

class Node:
    def __init__(self, start):
        self.start, self.end, self.items = start, None, []  # items: Node or (kind, text, start, end)

    @property
    def head(self):
        first = self.items[0] if self.items else None
        return first[1] if isinstance(first, tuple) and first[0] == "atom" else None

    def children(self, head=None):
        return [i for i in self.items if isinstance(i, Node) and (head is None or i.head == head)]

    def strings(self):
        return [i for i in self.items if isinstance(i, tuple) and i[0] == "str"]


def parse(text):
    stack, root, i, n = [], None, 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            node = Node(i)
            if stack:
                stack[-1].items.append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            node = stack.pop()
            node.end = i + 1
            if not stack:
                root = node
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            stack[-1].items.append(("str", "".join(buf), i, j + 1))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            stack[-1].items.append(("atom", text[i:j], i, j))
            i = j
    if root is None or root.head != "kicad_sch":
        raise ValueError("not a KiCad schematic")
    return root


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def key(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def root_schematic(target):
    target = Path(target)
    if target.is_dir():
        projects = sorted(target.glob("*.kicad_pro"))
        if len(projects) != 1:
            raise ValueError(f"Expected one .kicad_pro in {target}, found {len(projects)}; pass the .kicad_pro or root .kicad_sch")
        target = projects[0]
    root = target.with_suffix(".kicad_sch") if target.suffix == ".kicad_pro" else target
    if root.suffix != ".kicad_sch" or not root.is_file():
        raise ValueError(f"Root schematic not found: {root}")
    return root.resolve()


def design_files(root):
    """Root schematic plus every sub-sheet file it references (what KiCad loads)."""
    files, queue = [], [root]
    while queue:
        path = queue.pop(0)
        if path in files:
            continue
        files.append(path)
        tree = parse(path.read_text(encoding="utf-8"))
        for sheet in tree.children("sheet"):
            for prop in sheet.children("property"):
                strings = prop.strings()
                if len(strings) >= 2 and key(strings[0][1]) == "sheetfile":
                    child = (path.parent / strings[1][1]).resolve()
                    if not child.is_file():
                        raise ValueError(f"Sub-sheet referenced by {path.name} not found: {child}")
                    queue.append(child)
    return files
