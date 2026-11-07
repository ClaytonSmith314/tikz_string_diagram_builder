#!/usr/bin/env python3
"""
string_diagram_gen.py - generate TikZiT-compatible .tikz string diagrams
(Markov-category style) from a compact text description.

Usage:
    string_diagram_gen.py "f(y:Y|x) g(z:Z|x); h(x|a:A)"
    string_diagram_gen.py -f diagram.txt -o my_diagram.tikz
    string_diagram_gen.py                      (prompts for a string)

Syntax:
    f(y:Y|x)          morphism with label f, output y (object Y), input x
    f(y1, y2|x1, x2)  outputs / inputs are listed left to right
    p(x) or p(x|)     distribution (no inputs)
    e(|x)             effect (no outputs)
    (f \\circ g)(y|x)  parenthesised label for more complex LaTeX
    space or comma    separates morphisms on the same row
    ;                 starts a new row (rows are drawn top to bottom)
    \\copy(x1,x2|x)    explicit copy (any number of outputs)
    \\del(x)           explicit delete
    \\1(x)             identity: forces string x to pass through this row
                      at this position
    { d1 } = { d2 }   equation: diagrams side by side, separated by any
                      LaTeX relation (=, \\approx, \\overset{a}{=}, ...)

Strings flow upwards: the first row is the top of the diagram, so a
variable must be produced on a lower row (later in the string) than the
rows that use it. A variable used several times is copied implicitly,
variables never produced are diagram inputs, and outputs never used are
diagram outputs.

The generator works in four steps:
    1. parse      - rows, morphisms and variable connections
    2. structure  - wires, implicit copies, pass-throughs and string order
    3. layout     - element sizes and x/y positions
    4. emit       - the .tikz file
"""

import argparse
import re
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Layout constants (cm)
# ---------------------------------------------------------------------------
S = 0.5            # distance between adjacent strings
BOX_H = 0.6        # morphism box height
BOX_MIN_W = 0.6    # minimum morphism box width
BOX_PAD = 0.35     # horizontal padding around box text
BOX_GAP = 0.2      # space between a box and its neighbours in a row
COPY_H = 0.4       # height of a row containing a copy
COPY_DOT = 0.1     # height of the copy dot above the bottom of its row
DEL_H = 0.35       # height of a row containing a delete
DEL_DOT = 0.25     # height of the delete dot above the bottom of its row
GAP_MIN = 0.5      # minimum vertical gap between rows
LIGHT_H = 0.25     # height of a row without morphism boxes (copies/deletes)
LIGHT_GAP = 0.3    # minimum gap next to a row without morphism boxes
CHAIN_W = 20.0     # pull keeping a string that passes through rows straight
COPY_OUT_W = 0.25  # pull of a copy's branches (copies sit above their input)
CROSS_W = 0.1      # light pull on strings that cross (keeps crossings compact)
CURVE_CLEAR = 0.12 # clearance of an extended crossing curve from other elements
EQ_GAP = 0.6       # space between a diagram and an equation's relation symbol
L2_ITERS = 600     # layout iterations, smooth phase
L1_ITERS = 1500    # layout iterations, straightening phase
L1_EPS = 0.01      # offset below which a string counts as straight
GAP_BASE = 0.25    # gap height = GAP_BASE + GAP_SLOPE * (max horizontal shift)
GAP_SLOPE = 0.55
STUB = 0.5         # length of diagram input/output strings
LABEL_OFF = 0.3    # distance from string end to boundary object label
INNER_LABEL_DX = 0.08  # offset of internal object labels from their string
CHAR_W = 0.2       # estimated width of one math glyph
SYM_W = 0.3        # estimated width of a LaTeX command / operator glyph


class DiagramError(Exception):
    pass


class Elem:
    """A row element: morphism box, copy, delete, identity or pass-through."""

    def __init__(self, kind, label='', in_vars=(), out_vars=(), fixed=True):
        self.kind = kind            # 'box' | 'copy' | 'del' | 'id' | 'pass'
        self.label = label
        self.in_vars = list(in_vars)
        self.out_vars = list(out_vars)
        self.ins = []               # Wire objects (step 2)
        self.outs = []
        self.fixed = fixed          # False: position chosen by the generator
        self.row = None
        self.x = 0.0

    def name(self):
        return {'copy': '\\copy', 'del': '\\del', 'id': '\\1',
                'pass': 'pass-through'}.get(self.kind, self.label)


class Wire:
    """A string from src (None = diagram input) to dst (None = diagram output)."""

    def __init__(self, var, src, dst):
        self.var = var
        self.src = src
        self.dst = dst


# ---------------------------------------------------------------------------
# Step 1: parsing
# ---------------------------------------------------------------------------

def split_top(s, seps):
    """Split s on characters in seps that are not nested in brackets."""
    parts, cur, depth = [], [], 0
    for ch in s:
        if ch in '({[':
            depth += 1
        elif ch in ')}]':
            depth -= 1
        if depth == 0 and ch in seps:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return parts


def read_group(s, i):
    """s[i] == '('; return (contents, index after the matching ')')."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '(':
            depth += 1
        elif s[j] == ')':
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
    raise DiagramError(f"unbalanced parenthesis in {s[i:].strip()!r}")


def parse_ports(s, where):
    s = s.strip()
    if not s:
        return []
    ports = []
    for item in split_top(s, ','):
        item = item.strip()
        var, obj = (item.split(':', 1) + [None])[:2]
        var = var.strip()
        obj = obj.strip() if obj and obj.strip() else None
        if not var or re.search(r'[\s(){}\[\]|;:]', var):
            raise DiagramError(f"bad variable name {var!r} in {where}")
        ports.append((var, obj))
    return ports


def make_elems(label, args):
    where = f"{label}({args})"
    parts = split_top(args, '|')
    if len(parts) > 2:
        raise DiagramError(f"more than one '|' in {where}")
    outs = parse_ports(parts[0], where)
    ins = parse_ports(parts[1], where) if len(parts) == 2 else []

    if label == '\\copy':
        if len(ins) != 1 or not outs:
            raise DiagramError(f"\\copy needs one input and at least one output: {where}")
        return [Elem('copy', label, ins, outs)]
    if label == '\\del':
        if len(parts) == 2 and outs:
            raise DiagramError(f"\\del has no outputs: {where}")
        return [Elem('del', label, [p], []) for p in (ins if len(parts) == 2 else outs)]
    if label in ('\\1', '\\id'):
        if len(parts) == 2 and [v for v, _ in outs] != [v for v, _ in ins]:
            raise DiagramError(f"\\1 must have the same inputs and outputs: {where}")
        return [Elem('id', label, [p], [p]) for p in outs]
    if not label:
        raise DiagramError(f"morphism without a label: {where}")
    return [Elem('box', label, ins, outs)]


def parse_row(text):
    elems, i, n = [], 0, len(text)
    while i < n:
        if text[i].isspace() or text[i] == ',':
            i += 1
            continue
        if text[i] == '(':
            label, i = read_group(text, i)
            label = label.strip()
        else:
            j, depth = i, 0
            while j < n and not (text[j] == '(' and depth == 0):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                elif depth == 0 and (text[j].isspace() or text[j] == ','):
                    k = j
                    while k < n and text[k].isspace():
                        k += 1
                    if k < n and text[k] == '(':
                        break
                    raise DiagramError(f"expected '(' after {text[i:j]!r}")
                j += 1
            label, i = text[i:j].strip(), j
        while i < n and text[i].isspace():
            i += 1
        if i >= n or text[i] != '(':
            raise DiagramError(f"{label!r} is missing its argument list '(...)'")
        args, i = read_group(text, i)
        elems.extend(make_elems(label, args))
    return elems


def parse(text):
    """Return (rows bottom-up, objects, base): rows are lists of Elems with
    in_vars/out_vars as plain names; objects maps user variables to object
    labels; base maps internal variable names to the user's name."""
    text = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#'))
    rows_td = [parse_row(chunk) for chunk in split_top(text, ';') if chunk.strip()]
    rows_td = [r for r in rows_td if r]
    if not rows_td:
        raise DiagramError("empty diagram")
    rows = rows_td[::-1]

    objects = {}
    for row in rows:
        for e in row:
            for v, o in e.in_vars + e.out_vars:
                if o is None:
                    continue
                if objects.get(v, o) != o:
                    raise DiagramError(f"variable {v!r} has two objects: {objects[v]!r} and {o!r}")
                objects[v] = o
            e.in_vars = [v for v, _ in e.in_vars]
            e.out_vars = [v for v, _ in e.out_vars]

    # Identities rename their variable so that later uses refer to them.
    current, base, counter = {}, {}, 0
    for r, row in enumerate(rows):
        renamed = {}
        for e in row:
            e.row = r
            e.in_vars = [current.get(v, v) for v in e.in_vars]
            if e.kind == 'id':
                orig = e.out_vars[0]
                counter += 1
                new = f"{orig}@{counter}"
                e.out_vars = [new]
                base[new] = orig
                if orig in renamed:
                    raise DiagramError(f"\\1({orig}) appears twice on one row")
                renamed[orig] = new
        current.update(renamed)
    return rows, objects, base


# ---------------------------------------------------------------------------
# Step 2: structure (wires, implicit copies, pass-throughs, ordering)
# ---------------------------------------------------------------------------

def build_structure(rows, base):
    disp = lambda v: base.get(v, v)
    producer, consumers, var_order = {}, defaultdict(list), []
    for row in reversed(rows):
        for e in row:
            for port, v in enumerate(e.out_vars):
                if v in producer:
                    raise DiagramError(f"variable {disp(v)!r} is output by two morphisms")
                producer[v] = (e, port)
                var_order.append(v)
            for port, v in enumerate(e.in_vars):
                consumers[v].append((e, port))
                var_order.append(v)
    for v, cons in consumers.items():
        if v in producer:
            pe = producer[v][0]
            for ce, _ in cons:
                if ce.row <= pe.row:
                    raise DiagramError(
                        f"{disp(v)!r} is an input of {ce.name()!r} but is produced by "
                        f"{pe.name()!r} on the same or a higher row; producers must be on "
                        f"lower rows (after a later ';')")

    for row in rows:
        for e in row:
            e.ins = [None] * len(e.in_vars)
            e.outs = [None] * len(e.out_vars)

    wires = []
    copies_in_row = defaultdict(list)   # existing row index -> copies
    copies_after = defaultdict(list)    # new row inserted above row index
    seen = set()
    for v in var_order:
        if v in seen:
            continue
        seen.add(v)
        src, sport = producer.get(v, (None, None))
        p = src.row if src else -1
        cons = consumers.get(v, [])
        if len(cons) <= 1:
            dst, dport = cons[0] if cons else (None, None)
            w = Wire(v, src, dst)
            wires.append(w)
            if src:
                src.outs[sport] = w
            if dst:
                dst.ins[dport] = w
            continue
        c = Elem('copy', fixed=False)
        w = Wire(v, src, c)
        wires.append(w)
        if src:
            src.outs[sport] = w
        c.ins = [w]
        for ce, cport in cons:
            cw = Wire(v, c, ce)
            wires.append(cw)
            c.outs.append(cw)
            ce.ins[cport] = cw
        if min(ce.row for ce, _ in cons) == p + 1:
            copies_after[p].append(c)
        else:
            copies_in_row[p + 1].append(c)

    new_rows = []
    for r in range(-1, len(rows)):
        if r >= 0:
            new_rows.append(rows[r] + copies_in_row[r])
        if copies_after[r]:
            new_rows.append(list(copies_after[r]))
    for r, row in enumerate(new_rows):
        for e in row:
            e.row = r
    rows = new_rows
    n = len(rows)

    for w in wires:
        rs = w.src.row if w.src else -1
        rd = w.dst.row if w.dst else n
        for r in range(rs + 1, rd):
            pe = Elem('pass', fixed=False)
            pe.ins, pe.outs, pe.row = [w], [w], r
            rows[r].append(pe)

    order_rows(rows, wires)
    return rows, wires


def arrange(row, ref, side, sec, sec_side):
    """Order a row: fixed elements keep their order, flexible ones (copies,
    pass-throughs) are inserted where they cause the fewest crossings
    against `ref` (the string order on `side`), ties broken by `sec`."""
    idx = {w: i for i, w in enumerate(ref)}
    sidx = {w: i for i, w in enumerate(sec or [])}
    side_w = lambda e, s: e.outs if s == 'above' else e.ins

    for e in row:
        if e.kind == 'copy' and not e.fixed:
            if side == 'above':
                e.outs.sort(key=lambda w: idx[w])
            elif sec:
                e.outs.sort(key=lambda w: sidx.get(w, -1))

    def before_cost(a, b, ix):   # crossings if wire list a is left of b
        return sum(1 for x in a for y in b if x in ix and y in ix and ix[x] > ix[y])

    def bary(ws, ix):
        vals = [ix[w] for w in ws if w in ix]
        return sum(vals) / len(vals) if vals else 0.0

    fixed = [e for e in row if e.fixed]
    slots = [[] for _ in range(len(fixed) + 1)]
    for e in row:
        if e.fixed:
            continue
        a, b = side_w(e, side), side_w(e, sec_side)
        best = None
        for s in range(len(fixed) + 1):
            c1 = (sum(before_cost(side_w(f, side), a, idx) for f in fixed[:s]) +
                  sum(before_cost(a, side_w(f, side), idx) for f in fixed[s:]))
            c2 = (sum(before_cost(side_w(f, sec_side), b, sidx) for f in fixed[:s]) +
                  sum(before_cost(b, side_w(f, sec_side), sidx) for f in fixed[s:]))
            if best is None or (c1, c2) < best[0]:
                best = ((c1, c2), s)
        slots[best[1]].append(e)

    out = []
    for s, slot in enumerate(slots):
        slot.sort(key=lambda e: (bary(side_w(e, side), idx), bary(side_w(e, sec_side), sidx)))
        out.extend(slot)
        if s < len(fixed):
            out.append(fixed[s])
    return out


def order_rows(rows, wires):
    """Choose element order in each row. Diagram outputs are first placed by
    a bottom-up sweep (fewest crossings), then a final top-down sweep keeps
    the order demanded from above for as long as possible, so swaps happen
    at the lowest possible point."""
    n = len(rows)
    ins_of = lambda row: [w for e in row for w in e.ins]
    outs_of = lambda row: [w for e in row for w in e.outs]
    in_order, out_order = [None] * n, [None] * n

    def top_down(top_ref, sec):
        ref = top_ref
        for r in reversed(range(n)):
            rows[r][:] = arrange(rows[r], ref, 'above', sec[r] if sec else None, 'below')
            in_order[r], out_order[r] = ins_of(rows[r]), outs_of(rows[r])
            ref = in_order[r]

    def bottom_up(sec):
        ref = in_order[0]
        for r in range(n):
            rows[r][:] = arrange(rows[r], ref, 'below', sec[r], 'above')
            in_order[r], out_order[r] = ins_of(rows[r]), outs_of(rows[r])
            ref = out_order[r]

    pos = {e: i for row in rows for i, e in enumerate(row)}
    top = [w for w in wires if w.dst is None]
    top.sort(key=lambda w: (-w.src.row, pos[w.src], w.src.outs.index(w)))
    top_down(top, None)
    for _ in range(2):
        bottom_up(list(out_order))
        top_down(list(out_order[n - 1]), list(in_order))


# ---------------------------------------------------------------------------
# Step 3: sizing and spacing
# ---------------------------------------------------------------------------

FONT_CMDS = {'mathrm', 'mathbf', 'mathsf', 'mathit', 'mathcal', 'mathbb',
             'mathfrak', 'mathscr', 'text', 'textrm', 'operatorname',
             'boldsymbol', 'left', 'right', 'big', 'Big'}


def tex_width(s):
    w = 0.0
    for tok in re.findall(r'\\[A-Za-z]+|\\.|.', s.replace('$', '')):
        if tok.startswith('\\') and len(tok) > 1:
            if tok[1:] not in FONT_CMDS:
                w += SYM_W if tok[1:].isalpha() else 0.1
        elif tok in '{}^_ ':
            continue
        elif tok.isalnum():
            w += CHAR_W
        else:
            w += SYM_W
    return w


def centred(n):
    return [(j - (n - 1) / 2) * S for j in range(n)]


def set_geometry(e):
    if e.kind == 'box':
        nports = max(len(e.ins), len(e.outs), 1)
        e.width = max(BOX_MIN_W, tex_width(e.label) + BOX_PAD, nports * S)
        e.half, e.height = e.width / 2, BOX_H
        e.in_off, e.out_off = centred(len(e.ins)), centred(len(e.outs))
    elif e.kind == 'copy':
        k = len(e.outs)
        e.out_off = [(j - (k - 1) / 2) * S for j in range(k)]
        e.in_off, e.half, e.height = [0.0], (k - 1) * S / 2, COPY_H
    elif e.kind == 'del':
        e.in_off, e.out_off, e.half, e.height = [0.0], [], 0.0, DEL_H
    else:
        e.in_off, e.out_off, e.half, e.height = [0.0], [0.0], 0.0, 0.0


def sep(a, b):
    margin = BOX_GAP if 'box' in (a.kind, b.kind) else S
    return a.half + b.half + margin


def isotonic_fit(targets, weights, seps):
    """Minimise sum w_i (x_i - t_i)^2 subject to x_{i+1} - x_i >= seps_i."""
    offs = [0.0]
    for s in seps:
        offs.append(offs[-1] + s)
    blocks = []   # [weight, weighted sum, count]
    for t, w, o in zip(targets, weights, offs):
        blocks.append([w, w * (t - o), 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][0] > blocks[-1][1] / blocks[-1][0]:
            w2, s2, c2 = blocks.pop()
            blocks[-1][0] += w2
            blocks[-1][1] += s2
            blocks[-1][2] += c2
    ys = []
    for w, s, c in blocks:
        ys.extend([s / w] * c)
    return [y + o for y, o in zip(ys, offs)]


class Layout:
    def __init__(self, rows):
        self.rows = rows
        self.n = len(rows)
        for row in rows:
            for e in row:
                set_geometry(e)
        self.top_pt, self.bot_pt = {}, {}
        for r, row in enumerate(rows):
            for e in row:
                for w, o in zip(e.ins, e.in_off):
                    self.bot_pt[(w, r)] = (e, o)
                for w, o in zip(e.outs, e.out_off):
                    self.top_pt[(w, r)] = (e, o)
        self.crossed = self.find_crossed()
        self.place_x()
        self.place_y()

    def xat(self, pt):
        e, o = pt
        return e.x + o

    def place_x(self):
        for row in self.rows:
            x = 0.0
            for i, e in enumerate(row):
                if i:
                    x += sep(row[i - 1], e)
                e.x = x
            for e in row:
                e.x -= x / 2
        chain_of = self.find_chains()
        # Phase 1 minimises squared offsets (smooth, finds a good region);
        # phase 2 reweights to approximate absolute offsets, which prefers
        # strings that are exactly straight and concentrates the bends.
        for it in range(L2_ITERS + L1_ITERS):
            l1 = it >= L2_ITERS
            order = range(self.n) if it % 2 == 0 else reversed(range(self.n))
            for r in order:
                row = self.rows[r]
                targets, weights = [], []
                for e in row:
                    terms = self.pulls(e, r, chain_of)
                    if l1:
                        terms = [(t, w / max(abs(e.x - t), L1_EPS) if rw else w, rw)
                                 for t, w, rw in terms]
                    wsum = sum(w for _, w, _ in terms)
                    if wsum:
                        targets.append(sum(t * w for t, w, _ in terms) / wsum)
                        weights.append(wsum)
                    else:
                        targets.append(e.x)
                        weights.append(0.01)
                seps = [sep(row[i], row[i + 1]) for i in range(len(row) - 1)]
                for e, x in zip(row, isotonic_fit(targets, weights, seps)):
                    e.x = x
        for chain in {id(c): c for c in chain_of.values()}.values():
            self.straighten(chain)
        self.align_groups()
        xs = [e.x - e.half for row in self.rows for e in row] + \
             [e.x + e.half for row in self.rows for e in row]
        mid = (min(xs) + max(xs)) / 2
        for row in self.rows:
            for e in row:
                e.x = round(e.x - mid, 3)

    def find_crossed(self):
        """(wire, gap) pairs for strings that cross another string in that
        gap (gap g lies between rows g-1 and g). These may curve freely."""
        crossed = set()
        for g in range(1, self.n):
            below = {w: i for i, w in enumerate(w for e in self.rows[g - 1] for w in e.outs)}
            above = [w for e in self.rows[g] for w in e.ins if w in below]
            for i, v in enumerate(above):
                for w in above[i + 1:]:
                    if below[v] > below[w]:
                        crossed.update(((v, g), (w, g)))
        return crossed

    def seg_pull(self, w, gap, target, weight):
        """Pull term for string w in a gap: crossed strings get only a light
        pull that keeps the crossing compact, with no straightening. Copy
        branches (weight < 1) curve anyway, so they are not straightened."""
        if (w, gap) in self.crossed:
            return (target, CROSS_W, False)
        return (target, weight, weight >= 1.0)

    def pulls(self, e, r, chain_of):
        """Desired positions of e: list of (target x, weight, reweightable)."""
        chain = chain_of.get(e)
        if chain:
            return self.chain_terms(chain)
        win, wout = (1.0, COPY_OUT_W) if e.kind == 'copy' else (1.0, 1.0)
        terms = []
        for w, o in zip(e.ins, e.in_off):
            nb = self.top_pt.get((w, r - 1))
            if nb:
                terms.append(self.seg_pull(w, r, self.xat(nb) - o, win))
        for w, o in zip(e.outs, e.out_off):
            nb = self.bot_pt.get((w, r + 1))
            if nb:
                terms.append(self.seg_pull(w, r + 1, self.xat(nb) - o, wout))
        return terms

    def connections(self):
        """(lower elem, port offset, upper elem, port offset, weight, crossed)
        for every string segment between adjacent rows. Copy branches curve
        anyway, so their offset matters less."""
        conns = []
        for r, row in enumerate(self.rows[:-1]):
            for e in row:
                for w, o in zip(e.outs, e.out_off):
                    nb = self.bot_pt.get((w, r + 1))
                    if nb:
                        conns.append((e, o, nb[0], nb[1],
                                      COPY_OUT_W if e.kind == 'copy' else 1.0,
                                      (w, r + 1) in self.crossed))
        return conns

    def bend_cost(self, conns):
        cost = 0.0
        for a, oa, b, ob, wt, crossed in conns:
            dx = abs(a.x + oa - b.x - ob)
            if crossed:
                cost += CROSS_W * dx * dx
            elif wt < 1.0:      # copy branch: curves anyway, keep it balanced
                cost += wt * dx * dx
            elif dx > 1e-3:
                cost += wt * (1.0 + 0.3 * dx)
        return cost

    def align_groups(self):
        """Greedy pass: for each bent segment, try shifting the group of
        elements joined to one end by straight segments so that the segment
        becomes straight, pushing row neighbours along. Keep the move if it
        lowers the number of bent segments."""
        conns = self.connections()
        for _ in range(200):
            nbrs = defaultdict(list)
            for a, oa, b, ob, wt, crossed in conns:
                if wt >= 1.0 and not crossed and abs(a.x + oa - b.x - ob) <= 1e-3:
                    nbrs[a].append(b)
                    nbrs[b].append(a)
            base = self.bend_cost(conns)
            best = None
            moves = []
            for a, oa, b, ob, wt, crossed in conns:
                d = b.x + ob - a.x - oa
                if crossed or wt < 1.0 or abs(d) <= 1e-3:
                    continue
                for start, shift in ((a, d), (b, -d)):
                    moved = self.try_shift(start, shift, nbrs)
                    if moved is not None:
                        moves.append((moved, shift))
            # shift everything above a gap to centre the crossings in it
            for g in range(1, self.n):
                ds = [b.x + ob - a.x - oa for a, oa, b, ob, _, crossed in conns
                      if crossed and b.row == g]
                if ds and abs(sum(ds) / len(ds)) > 1e-3:
                    moves.append(({e for row in self.rows[g:] for e in row},
                                  -sum(ds) / len(ds)))
            for moved, shift in moves:
                old = {e: e.x for e in moved}
                for e in moved:
                    e.x += shift
                cost = self.bend_cost(conns)
                for e in moved:
                    e.x = old[e]
                if cost < base - 1e-6 and (best is None or cost < best[0]):
                    best = (cost, moved, shift)
            if best is None:
                return
            for e in best[1]:
                e.x += best[2]

    def try_shift(self, start, shift, nbrs):
        """Set of elements that must move by `shift` together with start:
        its straight-connected group plus anything it pushes in its rows."""
        moving = set()
        todo = [start]
        while todo:
            e = todo.pop()
            if e in moving:
                continue
            moving.add(e)
            todo.extend(nbrs[e])
            row = self.rows[e.row]
            i = row.index(e)
            step = 1 if shift > 0 else -1
            j = i + step
            while 0 <= j < len(row):
                f = row[j]
                if f in moving:
                    break
                left, right = (row[j - 1], f) if step > 0 else (f, row[j + 1])
                lx = left.x + (shift if left in moving else 0)
                rx = right.x + (shift if right in moving else 0)
                if rx - lx >= sep(left, right) - 1e-6:
                    break
                todo.append(f)
                break
            if len(moving) > 200:
                return None
        return moving

    def find_chains(self):
        """Group pass-throughs/identities on consecutive rows that carry one
        string into chains, which are drawn as a single straight line."""
        chain_of = {}
        for row in self.rows:
            for e in row:
                if e.kind not in ('pass', 'id') or e in chain_of:
                    continue
                chain = [e]
                while True:
                    nb = self.bot_pt.get((chain[-1].outs[0], chain[-1].row + 1))
                    if not nb or nb[0].kind not in ('pass', 'id'):
                        break
                    chain.append(nb[0])
                for c in chain:
                    chain_of[c] = chain
        return chain_of

    def chain_terms(self, chain):
        """Pulls shared by every element of a pass-through chain: one towards
        each end of the string (weaker from a copy branch, which curves
        anyway), plus a strong pull back when pushed outside the range
        between the ends, which would make the string bend back and forth.
        straighten() later aligns the chain's elements exactly."""
        lo = self.top_pt.get((chain[0].ins[0], chain[0].row - 1))
        hi = self.bot_pt.get((chain[-1].outs[0], chain[-1].row + 1))
        terms = []
        if lo:
            terms.append(self.seg_pull(chain[0].ins[0], chain[0].row, self.xat(lo),
                                       COPY_OUT_W if lo[0].kind == 'copy' else 1.0))
        if hi:
            terms.append(self.seg_pull(chain[-1].outs[0], chain[-1].row + 1, self.xat(hi), 1.0))
        if lo and hi:
            cur = sum(e.x for e in chain) / len(chain)
            a, b = sorted((self.xat(lo), self.xat(hi)))
            if not a <= cur <= b:
                terms.append((min(max(cur, a), b), CHAIN_W, True))
        return terms

    def straighten(self, chain):
        """Move every element of the chain to one x, if the rows allow it."""
        xs = sorted(e.x for e in chain)
        ends = [self.top_pt.get((chain[0].ins[0], chain[0].row - 1)),
                self.bot_pt.get((chain[-1].outs[0], chain[-1].row + 1))]
        mid = xs[len(xs) // 2]
        near = [self.xat(p) for p in ends if p and abs(self.xat(p) - mid) < 0.05]
        for cand in near + [mid] + xs:
            if all(self.fits(e, cand) for e in chain):
                for e in chain:
                    e.x = cand
                return

    def fits(self, e, x):
        row = self.rows[e.row]
        i = row.index(e)
        return ((i == 0 or x - row[i - 1].x >= sep(row[i - 1], e) - 1e-6) and
                (i == len(row) - 1 or row[i + 1].x - x >= sep(e, row[i + 1]) - 1e-6))

    def gap_dx(self, g):
        dx = 0.0
        if 0 < g < self.n:
            for e in self.rows[g]:
                for w, o in zip(e.ins, e.in_off):
                    nb = self.top_pt.get((w, g - 1))
                    if nb:
                        dx = max(dx, abs(e.x + o - self.xat(nb)))
        return dx

    def place_y(self):
        n = self.n
        self.y_bot, self.y_top = [0.0] * n, [0.0] * n
        self.has_box = [any(e.kind == 'box' for e in row) for row in self.rows]
        y = self.y_min = 0.0
        for r in range(n):
            if r == 0:
                y += STUB
            else:
                dx = self.gap_dx(r)
                gmin = GAP_MIN if self.has_box[r - 1] and self.has_box[r] else LIGHT_GAP
                y += max(gmin, GAP_BASE + GAP_SLOPE * dx) if dx > 1e-3 else gmin
            self.y_bot[r] = y
            heights = [e.height for e in self.rows[r]]
            y += max(heights, default=0.0) if self.has_box[r] else min(max(heights, default=0.0), LIGHT_H)
            self.y_top[r] = y
        self.y_max = y + STUB


# ---------------------------------------------------------------------------
# Step 4: TikZ output
# ---------------------------------------------------------------------------

def num(v):
    s = f"{v:.3f}".rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def math(s):
    return s if '$' in s else f"${s}$"


class TikzWriter:
    def __init__(self):
        self.nodes, self.edges, self.points = [], [], {}
        self.ox = self.oy = 0.0     # origin of the diagram being written

    def node(self, style, x, y, text=''):
        i = len(self.nodes)
        x, y = x + self.ox, y + self.oy
        self.nodes.append(f"\\node [{style}] ({i}) at ({num(x)}, {num(y)}) {{{text}}};")
        return str(i)

    def point(self, x, y):
        key = (num(x + self.ox), num(y + self.oy))
        if key not in self.points:
            self.points[key] = self.node('style=none', x, y) + '.center'
        return self.points[key]

    def edge(self, a, b, opts=None):
        o = f" [{opts}]" if opts else ''
        self.edges.append(f"\\draw{o} ({a}) to ({b});")

    def render(self):
        return '\n'.join(
            ["\\begin{tikzpicture}", "\t\\begin{pgfonlayer}{nodelayer}"] +
            ["\t\t" + s for s in self.nodes] +
            ["\t\\end{pgfonlayer}", "\t\\begin{pgfonlayer}{edgelayer}"] +
            ["\t\t" + s for s in self.edges] +
            ["\t\\end{pgfonlayer}", "\\end{tikzpicture}", ""])


def bezier_x(p0, p1, t):
    """(x, y) at t on TikZ's `in=-90, out=90` curve from p0 up to p1."""
    (x0, y0), (x1, y1) = p0, p1
    d = 0.3915 * ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    c0, c1 = (x0, y0 + d), (x1, y1 - d)
    u = 1 - t
    bx = u**3 * x0 + 3 * u * u * t * c0[0] + 3 * u * t * t * c1[0] + t**3 * x1
    by = u**3 * y0 + 3 * u * u * t * c0[1] + 3 * u * t * t * c1[1] + t**3 * y1
    return bx, by


def plan_crossing_curves(rows, wires, L, end_y):
    """For strings that cross in a gap and then run straight up to their end,
    extend the crossing curve over that straight part. Returns
    {wire: (gap, last absorbed row)}; the last row equals the destination
    row when the curve runs all the way to the end. A curve is only extended
    while it stays clear of the other elements in the rows it passes."""
    n = L.n
    vertical = lambda w, g: abs(L.xat(L.top_pt[(w, g - 1)]) - L.xat(L.bot_pt.get((w, g), L.top_pt[(w, g - 1)]))) < 0.02
    cands = {}
    for w in wires:
        rs = w.src.row if w.src else -1
        rd = w.dst.row if w.dst else n
        for g in range(rs + 1, rd + 1):
            if (w, g) in L.crossed:
                ends_in_dot = w.dst is not None and w.dst.kind in ('copy', 'del')
                if (g < rd or ends_in_dot) and all(vertical(w, h) for h in range(g + 1, rd + 1)):
                    cands[w] = (g, rd)
                break
    bundle = {}
    for w, (g, rd) in cands.items():
        for r in range(g, rd):
            bundle.setdefault(r, set()).add(L.bot_pt[(w, r)][0])

    def clear(w, g, k, p0, p1):
        for i in range(1, 50):
            x, y = bezier_x(p0, p1, i / 50)
            for r in range(g, k + 1):
                if not L.y_bot[r] - 0.05 <= y <= L.y_top[r] + 0.05:
                    continue
                row, own = rows[r], L.bot_pt[(w, r)][0]
                i0 = row.index(own)
                left = [e for e in row[:i0] if e not in bundle.get(r, ())]
                right = [e for e in row[i0 + 1:] if e not in bundle.get(r, ())]
                if left and x < left[-1].x + left[-1].half + CURVE_CLEAR:
                    return False
                if right and x > right[0].x - right[0].half - CURVE_CLEAR:
                    return False
        return True

    plan = {}
    for w, (g, rd) in cands.items():
        x0 = L.xat(L.top_pt[(w, g - 1)])
        xe = L.xat(L.bot_pt[(w, g)])
        for k in range(rd, g - 1, -1):
            y1 = end_y(w) if k == rd else L.y_top[k]
            if clear(w, g, min(k, rd - 1), (x0, L.y_top[g - 1]), (xe, y1)):
                plan[w] = (g, k)
                break
    return plan


def emit(rows, wires, objects, base, L, tw):
    n = L.n
    dots = {}

    for r, row in enumerate(rows):
        for e in row:
            if e.kind == 'box':
                h = L.y_top[r] - L.y_bot[r]
                style = f"style=morphism, minimum width={num(e.width)}cm, minimum height={num(h)}cm"
                tw.node(style, e.x, (L.y_bot[r] + L.y_top[r]) / 2, math(e.label))
            elif e.kind == 'copy':
                dots[e] = tw.node('style=bn', e.x, L.y_bot[r] + COPY_DOT)
            elif e.kind == 'del':
                dots[e] = tw.node('style=bn', e.x, L.y_bot[r] + min(DEL_DOT, L.y_top[r] - L.y_bot[r]))

    def gap_x(w, g):
        """x of wire w at the top and bottom of gap g (between rows g-1, g)."""
        lo = L.top_pt.get((w, g - 1))
        hi = L.bot_pt.get((w, g))
        xlo = L.xat(lo) if lo else L.xat(hi)
        xhi = L.xat(hi) if hi else xlo
        return xlo, xhi

    def end_y(w):
        if w.dst is None:
            return L.y_max
        r = w.dst.row
        if w.dst.kind == 'copy':
            return L.y_bot[r] + COPY_DOT
        if w.dst.kind == 'del':
            return L.y_bot[r] + min(DEL_DOT, L.y_top[r] - L.y_bot[r])
        return L.y_bot[r]

    plan = plan_crossing_curves(rows, wires, L, end_y)
    bkey = lambda v: base.get(v, v)
    boundary_bases = {bkey(w.var) for w in wires if w.src is None or w.dst is None}

    for w in wires:
        rs = w.src.row if w.src else -1
        rd = w.dst.row if w.dst else n
        obj = objects.get(bkey(w.var))
        chain = []   # list of (ref, x, y, segment kind leading to this point)

        x0 = gap_x(w, rs + 1)[0]
        if w.src is None:
            chain.append((None, x0, L.y_min, None))
            if obj:
                tw.node('style=none', x0, L.y_min - LABEL_OFF, math(obj))
        elif w.src.kind == 'copy':
            chain.append((dots[w.src], w.src.x, L.y_bot[rs] + COPY_DOT, None))
            off = x0 - w.src.x
            kind = 'line' if abs(off) < 0.02 else ('branch-r' if off > 0 else 'branch-l')
            chain.append((None, x0, L.y_top[rs], kind))
        elif w.src.kind == 'id':
            chain.append((None, x0, L.y_bot[rs], None))
            chain.append((None, x0, L.y_top[rs], 'line'))
        else:
            chain.append((None, x0, L.y_top[rs], None))

        for g in range(rs + 1, rd + 1):
            xlo, xhi = gap_x(w, g)
            ytop = L.y_bot[g] if g < n else L.y_max
            chain.append((None, xhi, ytop, 'line' if abs(xhi - xlo) < 0.02 else 'curve'))
            if g < rd and L.y_top[g] > L.y_bot[g]:
                chain.append((None, xhi, L.y_top[g], 'line'))

        if w.dst is None:
            if obj:
                tw.node('style=none', chain[-1][1], L.y_max + LABEL_OFF, math(obj))
        elif w.dst.kind == 'copy':
            chain.append((dots[w.dst], w.dst.x, L.y_bot[rd] + COPY_DOT, 'line'))
        elif w.dst.kind == 'del':
            chain.append((dots[w.dst], w.dst.x, L.y_bot[rd] + min(DEL_DOT, L.y_top[rd] - L.y_bot[rd]), 'line'))

        # extend a crossing curve over the straight part that follows it
        if w in plan:
            g, k = plan[w]
            i = next(i for i, p in enumerate(chain) if p[3] == 'curve' and p[2] == L.y_bot[g])
            j = len(chain) - 1 if k == rd else \
                next(j for j, p in enumerate(chain) if j > i and p[2] == L.y_top[k])
            chain[i:j + 1] = [chain[j][:3] + ('curve',)]

        # make nearly vertical straight segments exactly vertical
        for i in range(1, len(chain)):
            ref, x, y, kind = chain[i]
            if kind == 'line' and ref is None and abs(x - chain[i - 1][1]) < 0.02:
                chain[i] = (ref, chain[i - 1][1], y, kind)

        # merge consecutive vertical straight segments, and a copy branch
        # followed directly by a curve into a single branch
        merged = [chain[0]]
        for i in range(1, len(chain)):
            p = chain[i]
            prev = merged[-1][3]
            if (p[3] == 'line' and len(merged) > 1 and prev == 'line'
                    and merged[-1][0] is None
                    and abs(merged[-1][1] - p[1]) < 0.02 and abs(merged[-2][1] - p[1]) < 0.02):
                merged[-1] = p
            elif p[3] == 'curve' and prev and prev.startswith('branch'):
                merged[-1] = p[:3] + (prev,)
            else:
                merged.append(p)

        refs = [ref or tw.point(x, y) for ref, x, y, _ in merged]
        for i in range(1, len(merged)):
            kind = merged[i][3]
            dx = merged[i][1] - merged[i - 1][1]
            if kind == 'curve':
                tw.edge(refs[i - 1], refs[i], 'in=-90, out=90')
            elif kind and kind.startswith('branch') and abs(dx) >= 0.02:
                tw.edge(refs[i - 1], refs[i], f"in=-90, out={0 if kind == 'branch-r' else 180}")
            else:
                tw.edge(refs[i - 1], refs[i])

        # internal object label on the wire leaving the variable's producer
        if (obj and w.var == bkey(w.var) and bkey(w.var) not in boundary_bases
                and not (w.src and w.src.kind == 'copy' and not w.src.fixed)):
            spots = []
            for g in range(rs + 1, rd + 1):
                xlo, xhi = gap_x(w, g)
                spots.append((abs(xhi - xlo) >= 0.02, (xlo + xhi) / 2,
                              (L.y_top[g - 1] + L.y_bot[g]) / 2))
            _, lx, ly = min(spots, key=lambda s: s[0])
            tw.node('style=none, anchor=west', lx + INNER_LABEL_DX, ly, math(obj))


def split_equation(text):
    """'{ d1 } = { d2 } ...' -> ([d1, d2, ...], ['=', ...]); a plain
    diagram string gives ([text], []). A '{' starts a diagram when it is at
    the start, after whitespace, or after a non-word character, so LaTeX
    arguments such as \\mathrm{id} or \\overset{a}{=} are left alone."""
    t = text.strip()
    if not t.startswith('{'):
        return [text], []
    diagrams, seps, cur, i = [], [], [], 0
    while i < len(t):
        ch = t[i]
        starts = ch == '{' and (i == 0 or t[i - 1].isspace() or
                                not (t[i - 1].isalnum() or t[i - 1] in '}_^\\'))
        if not starts:
            cur.append(ch)
            i += 1
            continue
        depth = 0
        for j in range(i, len(t)):
            depth += {'{': 1, '}': -1}.get(t[j], 0)
            if depth == 0:
                break
        else:
            raise DiagramError("unbalanced '{' in equation")
        if diagrams:
            sep = ''.join(cur).strip()
            if not sep:
                raise DiagramError("missing relation (e.g. '=') between diagrams")
            seps.append(sep)
        elif ''.join(cur).strip():
            raise DiagramError(f"unexpected text before first diagram: {''.join(cur).strip()!r}")
        cur = []
        diagrams.append(t[i + 1:j])
        i = j + 1
    if ''.join(cur).strip():
        raise DiagramError(f"unexpected text after last diagram: {''.join(cur).strip()!r}")
    return diagrams, seps


def generate(text):
    diagrams, seps = split_equation(text)
    built = []
    for k, d in enumerate(diagrams):
        try:
            rows, objects, base = parse(d)                # step 1
            rows, wires = build_structure(rows, base)     # step 2
            layout = Layout(rows)                         # step 3
        except DiagramError as exc:
            if len(diagrams) > 1:
                raise DiagramError(f"diagram {k + 1}: {exc}") from None
            raise
        xs = [e.x - e.half for row in rows for e in row] + \
             [e.x + e.half for row in rows for e in row]
        built.append((rows, wires, objects, base, layout, min(xs), max(xs)))

    # place diagrams left to right with the relation symbols between them,
    # centred on x = 0 and each centred vertically on y = 0
    halves = [tex_width(sep) / 2 + EQ_GAP for sep in seps]
    total = sum(b[6] - b[5] for b in built) + 2 * sum(halves)
    tw = TikzWriter()
    x = -total / 2
    for k, (rows, wires, objects, base, layout, x0, x1) in enumerate(built):
        if k:
            tw.ox = tw.oy = 0.0
            tw.node('style=none', x + halves[k - 1], 0.0, math(seps[k - 1]))
            x += 2 * halves[k - 1]
        tw.ox, tw.oy = x - x0, -(layout.y_min + layout.y_max) / 2
        emit(rows, wires, objects, base, layout, tw)      # step 4
        x += x1 - x0
    return tw.render()


def main():
    ap = argparse.ArgumentParser(
        description="Generate a TikZiT .tikz string diagram from a text description.",
        epilog=__doc__.split('Syntax:')[1].split('The generator')[0].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('diagram', nargs='?', help='diagram string, e.g. "f(y:Y|x); p(x)"')
    ap.add_argument('-f', '--file', help='read the diagram string from a file')
    ap.add_argument('-o', '--output', default='string_diagram.tikz',
                    help='output file (default: string_diagram.tikz)')
    args = ap.parse_args()

    if args.file and args.diagram:
        ap.error("give either a diagram string or -f FILE, not both")
    if args.file:
        try:
            with open(args.file) as fh:
                text = fh.read()
        except OSError as exc:
            sys.exit(f"error: {exc}")
    elif args.diagram is not None:
        text = args.diagram
    else:
        try:
            text = input("String diagram: ")
        except EOFError:
            sys.exit(1)

    try:
        tikz = generate(text)
    except DiagramError as exc:
        sys.exit(f"error: {exc}")
    with open(args.output, 'w') as fh:
        fh.write(tikz)
    print(f"wrote {args.output}")


if __name__ == '__main__':
    main()