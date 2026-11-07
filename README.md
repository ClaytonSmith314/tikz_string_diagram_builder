# string_diagram_gen

Generate TikZiT-compatible `.tikz` string diagrams (Markov-category style) from
a compact one-line description. Single Python file, standard library only.

```
python3 string_diagram_gen.py 'f(y:Y|x) g(z:Z|x); h(x|a:A)'
python3 string_diagram_gen.py -f diagram.txt -o figure.tikz
python3 string_diagram_gen.py                 # prompts for a string
```

Output goes to `string_diagram.tikz` unless `-o` is given. Use single quotes in
the shell so backslashes (`\copy`, `\circ`, ...) survive. In `-f` files, lines
starting with `#` are ignored.

## Syntax

| Input | Meaning |
|---|---|
| `f(y:Y\|x)` | morphism `f`, output `y` with object `Y`, input `x` |
| `f(y1, y2\|x1, x2)` | outputs / inputs, left to right |
| `p(x)` or `p(x\|)` | distribution (no inputs) |
| `e(\|x)` | effect (no outputs) |
| `(f \circ g)(y\|x)` | parenthesised label for more complex LaTeX |
| space or `,` | separates morphisms on the same row |
| `;` | starts a new row; rows are drawn top to bottom |
| `\copy(x1, x2\|x)` | explicit copy (any number of outputs) |
| `\del(x)` | explicit delete |
| `\1(x)` | identity: forces string `x` through this row at this position |
| `{ d1 } = { d2 }` | equation; any LaTeX relation between the diagrams |

Variable names (`x`, `y`, ...) only define connections and are not rendered.
An object (`:Y`) is drawn as a label; give it on any one occurrence.

**Rules**

- Strings flow upwards, so a variable must be produced on a lower row (later in
  the string) than every row that uses it.
- A variable can be output by only one morphism.
- A variable used more than once is copied implicitly, as low as possible
  (adding a row if needed).
- Variables never produced are diagram inputs; outputs never used are diagram
  outputs.
- Swaps happen as low as possible; use `\1` to move one higher.

Examples:

```
f(z|x1, x2); \1(x2) g(x1|a); h(x2, a)
{ \1(x:X) f(y:Y|x); \psi(x) } = { f^\dag(x:X|y) \1(y:Y); \phi(y) }
```

## Using the output

The file contains only a `tikzpicture` using the `morphism` and `bn` styles
from `comonoids.tikzstyles` and the `nodelayer` / `edgelayer` layers, so it
opens in TikZiT and works with `tikzit.sty`:

```latex
\usepackage{tikzit}
\input{comonoids.tikzstyles}
...
\tikzfig{string_diagram}   % reads figures/string_diagram.tikz
```

Without `tikzit.sty`, declare the layers yourself:
`\pgfdeclarelayer{edgelayer}\pgfdeclarelayer{nodelayer}\pgfsetlayers{edgelayer,nodelayer,main}`.

## How it works

1. **Parse**: rows, morphisms and variable connections.
2. **Structure**: wires, implicit copies, pass-throughs and string order
   (fewest crossings, with swaps as low as possible).
3. **Layout**: box sizes and positions. Strings that do not cross are kept
   straight wherever spacing allows, and crossings are centred.
4. **Emit**: write the `.tikz` file. A crossing curve runs straight on to the
   string's end when there is room.

Spacing (string spacing, box height, gaps, ...) is set by the constants at the
top of `string_diagram_gen.py`.
