"""Source-level mutant generator for SystemVerilog RTL.

The mutator parses the RTL with slang (pyslang), walks the syntax tree and
collects mutation sites in run-time contexts only (continuous assignments,
procedural blocks other than initial/final, net declaration assignments,
instance port connections). Constant contexts are never mutated: parameter
and localparam values, declaration dimensions, range selects, generate
conditions, loop headers, case labels, replication counts, casts, system
function arguments, functions and tasks, packages and interfaces, and the
left-hand side of assignments.

Operator classes (fixed set):

  arith        + <-> -, * -> +, & <-> |, ^ -> |, << <-> >>, >>> -> >>, <<< -> <<
  relational   < -> <= / >, <= -> < / >=, > -> >= / <, >= -> > / <=,
               == <-> !=, && <-> ||
  constant     integer literal value +1 / -1 (size and base kept), '0 <-> '1
  negate       if / ternary condition c -> !(c)
  drop_term    A && B -> A or B, A || B -> A or B, reset condition -> 1'b0
  off_by_one   comparison bound R -> (R + 1) / (R - 1) when R is not a plain literal

Every mutant is a set of byte edits inside one expression node (its span).
Mutants are applied two ways:

  * standalone: the file with only that mutant's edits (equivalence check,
    report snippet)
  * schemata: one copy of the RTL where every selected span is replaced by
    ((pg_mut_sel == K) ? (mutated) : (original)), with pg_mut_sel read from
    the +PGMUT=<K> plusarg. One Verilator build then serves every mutant.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
import re
from pathlib import Path
from typing import Iterable

import pyslang as ps

K = ps.SyntaxKind

CLASSES = ["arith", "relational", "constant", "negate", "drop_term", "off_by_one"]

ARITH_SWAPS = {
    "+": ["-"], "-": ["+"], "*": ["+"], "&": ["|"], "|": ["&"], "^": ["|"],
    "<<": [">>"], ">>": ["<<"], ">>>": [">>"], "<<<": ["<<"],
}
REL_SWAPS = {
    "<": ["<=", ">"], "<=": ["<", ">="], ">": [">=", "<"], ">=": [">", "<="],
    "==": ["!="], "!=": ["=="], "&&": ["||"], "||": ["&&"],
}
REL_OPS = {"<", "<=", ">", ">=", "==", "!="}

RESET_RE = re.compile(r"(^|_)(rst|reset|clr|clear|srst|arst)(_|n|$)", re.I)
ENABLE_RE = re.compile(r"(^|_)(en|enable|valid|ready|we|wen|wr|rd|ren|load|ld|start|go|req|ack|vld|rdy)(_|$|n$)", re.I)

# Nodes whose whole subtree is a constant or non-synthesis context
SKIP_SUBTREE = {
    "ParameterDeclarationStatement", "ParameterDeclaration", "TypeParameterDeclaration",
    "ParameterPortList", "ParameterValueAssignment", "FunctionDeclaration",
    "TaskDeclaration", "PackageDeclaration", "InterfaceDeclaration", "ClassDeclaration",
    "ProgramDeclaration", "InitialBlock", "FinalBlock", "GenvarDeclaration",
    "IfGenerate", "LoopGenerate", "CaseGenerate", "ImmediateAssertStatement",
    "ImmediateAssumeStatement", "ImmediateCoverStatement", "AssertPropertyStatement",
    "AssumePropertyStatement", "CoverPropertyStatement", "PropertyDeclaration",
    "SequenceDeclaration", "ConcurrentAssertionMember", "ImmediateAssertionMember",
    "VariableDimension", "RangeDimensionSpecifier", "SimpleRangeSelect",
    "AscendingRangeSelect", "DescendingRangeSelect", "CastExpression",
    "SignedCastExpression",
    "EventControlWithExpression", "EventControl", "DelayControl",
    "BindDirective", "CheckerDeclaration", "ClockingDeclaration",
    "DefaultDisableDeclaration", "ModportDeclaration", "TypedefDeclaration",
    "DataType", "ImplicitType", "LogicType", "BitType", "RegType", "IntType",
    "IntegerType", "NamedType",
}
# Run-time contexts in which mutation sites are collected
RUNTIME_CTX = {
    "ContinuousAssign", "AlwaysBlock", "AlwaysCombBlock", "AlwaysFFBlock",
    "AlwaysLatchBlock", "NetDeclaration", "HierarchyInstantiation",
}
ASSIGN_KINDS = {
    "AssignmentExpression", "NonblockingAssignmentExpression",
    "AddAssignmentExpression", "SubtractAssignmentExpression",
    "MultiplyAssignmentExpression", "DivideAssignmentExpression",
    "AndAssignmentExpression", "OrAssignmentExpression", "XorAssignmentExpression",
    "LogicalLeftShiftAssignmentExpression", "LogicalRightShiftAssignmentExpression",
}
BINARY_OPS = {
    "AddExpression": "+", "SubtractExpression": "-", "MultiplyExpression": "*",
    "BinaryAndExpression": "&", "BinaryOrExpression": "|", "BinaryXorExpression": "^",
    "LogicalShiftLeftExpression": "<<", "LogicalShiftRightExpression": ">>",
    "ArithmeticShiftLeftExpression": "<<<", "ArithmeticShiftRightExpression": ">>>",
    "LessThanExpression": "<", "LessThanEqualExpression": "<=",
    "GreaterThanExpression": ">", "GreaterThanEqualExpression": ">=",
    "EqualityExpression": "==", "InequalityExpression": "!=",
    "LogicalAndExpression": "&&", "LogicalOrExpression": "||",
}
LITERAL_KINDS = {"IntegerLiteralExpression", "IntegerVectorExpression",
                 "UnbasedUnsizedLiteralExpression"}

SELECTOR = "pg_mut_sel"
INJECT = (f" integer {SELECTOR}; initial begin if (!$value$plusargs(\"PGMUT=%d\", {SELECTOR})) "
          f"{SELECTOR} = 0; end ")


@dataclasses.dataclass
class Mutant:
    file: str           # filelist-relative path, e.g. rtl/alu.sv
    line: int
    col: int
    op_class: str
    description: str
    span: tuple[int, int]               # byte range of the guarded expression
    edits: list[tuple[int, int, str]]   # byte edits within span
    module: str
    id: int = 0                          # 1..N once selected

    @property
    def key(self) -> str:
        e = ";".join(f"{a}:{b}:{r}" for a, b, r in self.edits)
        return f"{self.file}|{self.span[0]}|{self.span[1]}|{self.op_class}|{e}"

    def to_dict(self, sources: dict[str, bytes] | None = None) -> dict:
        d = {"id": self.id, "file": self.file, "line": self.line, "col": self.col,
             "class": self.op_class, "description": self.description,
             "module": self.module}
        if sources is not None:
            src = sources[self.file]
            d["original"] = src[self.span[0]:self.span[1]].decode(errors="replace")
            d["mutated"] = apply_edits(src[self.span[0]:self.span[1]],
                                       [(a - self.span[0], b - self.span[0], r)
                                        for a, b, r in self.edits]).decode(errors="replace")
        return d


def apply_edits(text: bytes, edits: Iterable[tuple[int, int, str]]) -> bytes:
    out = text
    for a, b, r in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:a] + r.encode() + out[b:]
    return out


# --------------------------------------------------------------------------
# Site collection
# --------------------------------------------------------------------------

def _kind(node) -> str:
    return str(node.kind).split(".")[-1]


def _rng(node) -> tuple[int, int]:
    r = node.sourceRange
    return r.start.offset, r.end.offset


def _identifiers(node) -> set[str]:
    out: set[str] = set()

    def visit(n):
        if isinstance(n, ps.Token):
            if str(n.kind).endswith("Identifier"):
                out.add(n.valueText)
            return
        for c in n:
            if c is not None:
                visit(c)
    visit(node)
    return out


def _constant_names(module) -> set[str]:
    names: set[str] = set()

    def visit(n):
        if isinstance(n, ps.Token) or n is None:
            return
        k = _kind(n)
        if k in ("ParameterDeclaration", "TypeParameterDeclaration"):
            for d in n.declarators:
                if hasattr(d, "name"):
                    names.add(d.name.valueText)
        elif k == "GenvarDeclaration":
            for ident in n.identifiers:
                if hasattr(ident, "identifier"):
                    names.add(ident.identifier.valueText)
        elif k == "LoopGenerate":
            try:
                names.add(n.identifier.valueText)
            except AttributeError:
                pass
        for c in n:
            visit(c)
    visit(module)
    return names


class _Collector:
    def __init__(self, file_rel: str, text: bytes, tree, reset_names: set[str]):
        self.file = file_rel
        self.text = text
        self.tree = tree
        self.sm = tree.sourceManager
        self.reset_names = reset_names
        self.sites: list[Mutant] = []
        self.modules: list[tuple[str, int]] = []  # (name, injection offset)
        self.buffer = None

    # A node is usable only if it maps 1:1 onto this file's bytes.
    def _in_file(self, node) -> bool:
        r = node.sourceRange
        if not self.sm.isFileLoc(r.start) or not self.sm.isFileLoc(r.end):
            return False
        if self.buffer is None:
            return False
        if r.start.buffer != self.buffer or r.end.buffer != self.buffer:
            return False
        a, b = r.start.offset, r.end.offset
        return 0 <= a < b <= len(self.text)

    def _line_col(self, offset: int) -> tuple[int, int]:
        line = self.text.count(b"\n", 0, offset) + 1
        col = offset - (self.text.rfind(b"\n", 0, offset) + 1) + 1
        return line, col

    def _add(self, cls: str, node, edits, desc: str, module: str):
        span = _rng(node)
        if not self._in_file(node):
            return
        for a, b, _ in edits:
            if not (span[0] <= a <= b <= span[1]):
                return
        line, col = self._line_col(edits[0][0] if edits else span[0])
        self.sites.append(Mutant(self.file, line, col, cls, desc, span, list(edits), module))

    def run(self):
        root = self.tree.root
        first = root.getFirstToken()
        if first is not None:
            self.buffer = first.location.buffer
        members = root.members if _kind(root) == "CompilationUnit" else [root]
        for member in members:
            k = _kind(member)
            if k == "ModuleDeclaration":
                name = member.header.name.valueText
                semi = member.header.semi
                if semi is None or semi.isMissing or not self.sm.isFileLoc(semi.location):
                    continue
                self.modules.append((name, semi.location.offset + 1))
                consts = _constant_names(member)
                self._walk(member.members, False, False, name, consts)

    # ctx: runtime context reached; lhs: inside an assignment target
    def _walk(self, node, runtime: bool, lhs: bool, module: str, consts: set[str]):
        if node is None or isinstance(node, ps.Token):
            return
        k = _kind(node)
        if k in SKIP_SUBTREE:
            return
        if k in RUNTIME_CTX:
            runtime = True
        if k == "ForLoopStatement":
            self._walk(node.statement, runtime, lhs, module, consts)
            return
        if k == "StandardCaseItem":
            self._walk(node.clause, runtime, lhs, module, consts)
            return
        if k == "CaseStatement":
            for it in node.items:
                self._walk(it, runtime, lhs, module, consts)
            return
        if k == "MultipleConcatenationExpression":
            self._walk(node.concatenation, runtime, lhs, module, consts)
            return
        if k == "InvocationExpression":
            left = str(node.left).strip()
            if left.startswith("$"):
                return
        if k in ASSIGN_KINDS:
            self._walk(node.left, runtime, True, module, consts)
            self._walk(node.right, runtime, lhs, module, consts)
            return
        if k == "NamedPortConnection" or k == "OrderedPortConnection":
            # port connections: outputs are LHS-like, inputs runtime; mutate
            # only expressions that are not plain names
            pass
        if runtime and not lhs:
            self._sites_for(node, k, module, consts)
        if k == "ConditionalStatement":
            for cond in node.predicate.conditions:
                self._walk(cond.expr, runtime, lhs, module, consts)
            self._walk(node.statement, runtime, lhs, module, consts)
            if node.elseClause is not None:
                self._walk(node.elseClause, runtime, lhs, module, consts)
            return
        for c in node:
            self._walk(c, runtime, lhs, module, consts)

    def _is_const(self, node, consts) -> bool:
        if _kind(node) in LITERAL_KINDS:
            return True
        ids = _identifiers(node)
        return bool(ids) and ids <= consts

    def _sites_for(self, node, k: str, module: str, consts: set[str]):
        if k in BINARY_OPS:
            op = BINARY_OPS[k]
            tok = node.operatorToken
            if self._is_const(node, consts):
                return  # e.g. W-1 in a run-time select
            a, b = tok.range.start.offset, tok.range.end.offset
            for new in ARITH_SWAPS.get(op, []):
                self._add("arith", node, [(a, b, new)], f"'{op}' -> '{new}'", module)
            for new in REL_SWAPS.get(op, []):
                self._add("relational", node, [(a, b, new)], f"'{op}' -> '{new}'", module)
            if op in ("&&", "||"):
                la, lb = _rng(node.left)
                ra, rb = _rng(node.right)
                rtxt = self.text[ra:rb].decode(errors="replace")
                ltxt = self.text[la:lb].decode(errors="replace")
                # drop right term: delete from end of left to end of right
                self._add("drop_term", node, [(lb, rb, "")], f"drop term '{rtxt.strip()}'", module)
                self._add("drop_term", node, [(la, ra, "")], f"drop term '{ltxt.strip()}'", module)
            if op in REL_OPS:
                left, right = node.left, node.right
                lc, rc = self._is_const(left, consts), self._is_const(right, consts)
                bound = left if (lc and not rc) else right
                if _kind(bound) not in LITERAL_KINDS:
                    ba, bb = _rng(bound)
                    for d in ("+", "-"):
                        self._add("off_by_one", node, [(ba, ba, "("), (bb, bb, f" {d} 1)")],
                                  f"bound {d}1", module)
            return
        if k in LITERAL_KINDS:
            self._literal_sites(node, k, module)
            return
        if k == "ConditionalStatement":
            for cond in node.predicate.conditions:
                e = cond.expr
                if cond.matchesClause is not None:
                    continue
                ea, eb = _rng(e)
                self._add("negate", e, [(ea, ea, "!("), (eb, eb, ")")], "negated condition", module)
                ids = _identifiers(e)
                if any(RESET_RE.search(i) or i in self.reset_names for i in ids):
                    self._add("drop_term", e, [(ea, eb, "1'b0")], "reset branch removed", module)
            return
        if k == "ConditionalExpression":
            for cond in node.predicate.conditions:
                e = cond.expr
                if cond.matchesClause is not None:
                    continue
                ea, eb = _rng(e)
                self._add("negate", e, [(ea, ea, "!("), (eb, eb, ")")], "negated condition", module)
            return

    def _literal_sites(self, node, k: str, module: str):
        a, b = _rng(node)
        raw = self.text[a:b].decode(errors="replace")
        if k == "UnbasedUnsizedLiteralExpression":
            flip = {"'0": "'1", "'1": "'0"}.get(raw.strip())
            if flip:
                self._add("constant", node, [(a, b, flip)], f"{raw} -> {flip}", module)
            return
        if k == "IntegerLiteralExpression":
            if not re.fullmatch(r"\d+", raw.replace("_", "")):
                return
            v = int(raw.replace("_", ""))
            for nv in ([v + 1, v - 1] if v > 0 else [v + 1]):
                self._add("constant", node, [(a, b, str(nv))], f"{v} -> {nv}", module)
            return
        # sized / based vector: size 'sb value
        m = re.fullmatch(r"\s*(\d*)\s*'\s*([sS]?)([bBoOdDhH])\s*([0-9a-fA-F_]+)\s*", raw)
        if not m:
            return
        size_s, sgn, base, digits = m.groups()
        radix = {"b": 2, "o": 8, "d": 10, "h": 16}[base.lower()]
        try:
            v = int(digits.replace("_", ""), radix)
        except ValueError:
            return
        size = int(size_s) if size_s else 32
        mask = (1 << size) - 1 if size < 4096 else None
        cands = [v + 1, v - 1] if v > 0 else [v + 1]
        for nv in cands:
            if mask is not None:
                nv &= mask
            if nv == v:
                continue
            fmt = {2: "b", 8: "o", 10: "d", 16: "h"}[radix]
            digits_new = format(nv, {2: "b", 8: "o", 10: "d", 16: "x"}[radix])
            ndig = len(digits.replace("_", ""))
            if radix != 10 and len(digits_new) < ndig:
                digits_new = digits_new.zfill(ndig)
            if digits.isupper():
                digits_new = digits_new.upper()
            new = f"{size_s}'{sgn}{fmt}{digits_new}"
            self._add("constant", node, [(a, b, new)], f"{raw.strip()} -> {new}", module)


def collect_sites(sources: dict[str, bytes], reset_names: Iterable[str] = ()) -> tuple[list[Mutant], dict[str, list[tuple[str, int]]]]:
    """Return (all candidate mutants, per-file module injection points)."""
    sites: list[Mutant] = []
    modules: dict[str, list[tuple[str, int]]] = {}
    for rel in sorted(sources):
        text = sources[rel]
        tree = ps.SyntaxTree.fromText(text.decode(errors="replace"), rel)
        if len(text.decode(errors="replace").encode()) != len(text):
            continue  # non UTF-8 bytes: offsets unreliable, skip file
        col = _Collector(rel, text, tree, set(reset_names))
        col.run()
        sites.extend(col.sites)
        modules[rel] = col.modules
    # dedupe identical edits
    seen = set()
    uniq = []
    for s in sites:
        mutated = apply_edits(sources[s.file], s.edits)
        h = (s.file, hashlib.sha1(mutated).hexdigest())
        if h in seen:
            continue
        seen.add(h)
        uniq.append(s)
    return uniq, modules


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def ordered_candidates(sites: list[Mutant], seed: int) -> list[Mutant]:
    """Deterministic, class-balanced order: round robin over shuffled classes."""
    rng = random.Random(seed)
    by_class: dict[str, list[Mutant]] = {c: [] for c in CLASSES}
    for s in sorted(sites, key=lambda m: m.key):
        by_class[s.op_class].append(s)
    for c in CLASSES:
        rng.shuffle(by_class[c])
    order = list(CLASSES)
    rng.shuffle(order)
    out: list[Mutant] = []
    idx = {c: 0 for c in CLASSES}
    while True:
        progressed = False
        for c in order:
            if idx[c] < len(by_class[c]):
                out.append(by_class[c][idx[c]])
                idx[c] += 1
                progressed = True
        if not progressed:
            break
    return out


def standalone(sources: dict[str, bytes], m: Mutant) -> dict[str, bytes]:
    out = dict(sources)
    out[m.file] = apply_edits(sources[m.file], m.edits)
    return out


def slang_errors(sources: dict[str, bytes], top: str | None = None) -> list[str]:
    """Semantic check of a full source set with slang. Returns error strings."""
    comp = ps.Compilation()
    for rel in sorted(sources):
        comp.addSyntaxTree(ps.SyntaxTree.fromText(sources[rel].decode(errors="replace"), rel))
    errs = []
    for d in comp.getAllDiagnostics():
        if d.isError():
            errs.append(str(d.code))
    return errs


def instantiated_modules(sources: dict[str, bytes], top: str) -> set[str]:
    """Modules actually elaborated under `top` (disabled generate branches
    and unused modules excluded). Mutants elsewhere could never be killed."""
    opts = ps.CompilationOptions()
    opts.topModules = {top}
    comp = ps.Compilation(ps.Bag([opts]))
    for rel in sorted(sources):
        comp.addSyntaxTree(ps.SyntaxTree.fromText(sources[rel].decode(errors="replace"), rel))
    names: set[str] = set()

    def cb(sym):
        if sym.kind == ps.SymbolKind.Instance:
            names.add(sym.definition.name)
        return True
    for inst in comp.getRoot().topInstances:
        names.add(inst.definition.name)
        inst.visit(cb)
    return names


# --------------------------------------------------------------------------
# Schemata rendering
# --------------------------------------------------------------------------

def _contains(outer: Mutant, inner: Mutant) -> bool:
    return outer.span[0] <= inner.span[0] and inner.span[1] <= outer.span[1]


def _render(text: bytes, lo: int, hi: int, items: list[Mutant]) -> bytes:
    """Render text[lo:hi] with every mutant in items guarded."""
    items = sorted(items, key=lambda m: (m.span[0], -(m.span[1] - m.span[0]), m.id))
    tops: list[tuple[Mutant, list[Mutant]]] = []
    for m in items:
        if tops and _contains(tops[-1][0], m):
            tops[-1][1].append(m)
        else:
            tops.append((m, []))
    out = bytearray()
    pos = lo
    for m, children in tops:
        out += text[pos:m.span[0]]
        out += _guard(text, m, children)
        pos = m.span[1]
    out += text[pos:hi]
    return bytes(out)


def _guard(text: bytes, m: Mutant, children: list[Mutant]) -> bytes:
    a, b = m.span
    orig = _render(text, a, b, children)
    # mutated branch: apply m's edits; keep children that do not touch edits
    def touches(c: Mutant) -> bool:
        for ea, eb, _ in m.edits:
            if ea == eb:
                if c.span[0] < ea < c.span[1]:
                    return True
            elif not (eb <= c.span[0] or c.span[1] <= ea):
                return True
        return False
    keep = [c for c in children if not touches(c)]
    # build mutated text by walking segments between edits
    segs = bytearray()
    pos = a
    for ea, eb, r in sorted(m.edits, key=lambda e: (e[0], e[1])):
        segs += _render(text, pos, ea, [c for c in keep if pos <= c.span[0] and c.span[1] <= ea])
        segs += r.encode()
        pos = eb
    segs += _render(text, pos, b, [c for c in keep if pos <= c.span[0] and c.span[1] <= b])
    return (f"(({SELECTOR} == 32'd{m.id}) ? (".encode() + bytes(segs) + b") : ("
            + orig + b"))")


def schemata(sources: dict[str, bytes], mutants: list[Mutant],
             modules: dict[str, list[tuple[str, int]]]) -> dict[str, bytes]:
    """Return a source set in which all mutants are selectable by +PGMUT=K."""
    out = dict(sources)
    by_file: dict[str, list[Mutant]] = {}
    for m in mutants:
        by_file.setdefault(m.file, []).append(m)
    for rel, text in sources.items():
        ms = by_file.get(rel, [])
        inject_at = sorted({off for name, off in modules.get(rel, [])
                            if any(m.module == name for m in ms)})
        if not inject_at:
            out[rel] = _render(text, 0, len(text), ms) if ms else text
            continue
        # insert the selector declaration after each module header (outside
        # every span), shift the mutant offsets, then render the guards
        text2 = text
        for off in sorted(inject_at, reverse=True):
            text2 = text2[:off] + INJECT.encode() + text2[off:]
        shift = len(INJECT.encode())
        ms2 = []
        for m in ms:
            n_before = sum(1 for off in inject_at if off <= m.span[0])
            d = n_before * shift
            ms2.append(dataclasses.replace(
                m, span=(m.span[0] + d, m.span[1] + d),
                edits=[(x + d, y + d, r) for x, y, r in m.edits]))
        out[rel] = _render(text2, 0, len(text2), ms2)
    return out


def seed_from_hash(hexdigest: str) -> int:
    return int(hexdigest[:16], 16)


def load_sources(root: Path, files: list[str]) -> dict[str, bytes]:
    return {f: (root / f).read_bytes() for f in files}
