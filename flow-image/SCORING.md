# Scoring

Every scored submission gets

```
score = 100 × C × (0.5 V + 0.5 P)
```

with C, V and P each in [0, 1]. A rejected submission scores 0 and does not count toward the submission limit.

## C: correctness (hidden checks)

C = passed / total over the hidden checks of the design:

- every test of the hidden cocotb regression (including the tests written for the change request and one test per seeded deviation), and
- every labeled assertion and cover statement of the hidden SymbiYosys properties.

Each check counts once. A check that does not produce a result on your submission (the simulation crashed or timed out) counts as failed. The report lists failed checks by name only.

## V: mutation score

V = killed / N, where N is the number of mutants (40 unless the design has fewer valid mutation sites).

Mutants are made from your submitted RTL by one of six fixed operator classes:

| Class | Change |
|---|---|
| arith | `+` and `-` swapped, `*` to `+`, `&` and `|` swapped, `^` to `|`, `<<` and `>>` swapped |
| relational | `<`, `<=`, `>`, `>=` changed to a neighbor, `==` and `!=` swapped, `&&` and `||` swapped |
| constant | an integer literal changed by +1 or -1 (width and base kept), `'0` and `'1` swapped |
| negate | the condition of an `if` or `?:` negated |
| drop_term | one operand of `&&` or `||` dropped, or a reset branch made unreachable |
| off_by_one | the bound of a comparison changed by +1 or -1 |

Only run-time logic is mutated: continuous assignments, `always` blocks and port connections. Parameters, declarations, generate conditions, loop headers, case labels and function bodies are never mutated.

The set of mutants is reproducible: the random generator is seeded from the SHA-256 of your submission archive, and the classes are drawn in turn so that the set is balanced across classes. Mutants that the Flow proves equivalent to your design (formal equivalence check of the mutated module, with a short time limit per check and per submission) are discarded and replaced by the next candidate. Mutants inside modules that your top level does not instantiate are never selected.

A mutant is killed when, with your assertions bound, any of your tests fails, an assertion fires, the simulation crashes, or the run exceeds the per-mutant time limit (twice the run time of your tests on your own submission plus 15 seconds, at least 30 and at most 300 seconds). Only your tests in `tests/` and your assertions in `assertions/` are used; the public testbench is not part of your evidence unless you copy it into `tests/`.

The report lists each surviving mutant by file and line.

## P: PPA score

The Flow synthesizes your RTL with Yosys and places and routes it with OpenROAD on sky130hd under the released clock constraint, and compares the routed result with the baseline design run through the same script.

Let, from the final (post-route) report:

- A = standard-cell area, T = released clock period, S = worst setup slack (signed, ns), W = total power
- achievable period X = T - S (a design with positive slack could run faster than T)

and the relative improvements against the baseline (subscript b), each clipped to [-1, +1]:

```
a = clip((A_b - A) / A_b, -1, 1)
t = clip((X_b - X) / X_b, -1, 1)
p = clip((W_b - W) / W_b, -1, 1)
```

Then

```
P = clip(0.5 + 0.5 × (0.4 a + 0.4 t + 0.2 p), 0, 1)
if S < 0 after routing (timing not met):  P = min(P, 0.3)
```

Properties:

- P = 0.5 for a design identical in area, timing and power to the baseline.
- P increases when area, achievable period or power decreases, and decreases when they increase (monotone in each input).
- P is bounded in [0, 1]: 0 at the worst (every term at -1), 1 at the best.
- A design that fails timing after routing never scores above 0.3 on P.
- If place and route fails for your design, P = 0.

The report shows area, cell count, utilization, worst and total negative slack, power and wirelength at every stage (synthesis, floorplan, placement, clock tree, global route, detailed route, final) for your design and the baseline.

## Examples

| Submission | C | V | P | Score |
|---|---|---|---|---|
| Baseline unchanged, public tests copied as evidence | 0.7 | 0.4 | 0.5 | 31.5 |
| All deviations fixed, change request done, strong evidence, PPA unchanged | 1.0 | 0.9 | 0.5 | 70 |
| Same, 10% smaller and 5% faster | 1.0 | 0.9 | 0.53 | 71.5 |
| Correct but no tests or assertions | 1.0 | 0.0 | 0.5 | 25 |
| Area halved by deleting logic | 0.3 | 0.8 | 0.6 | 21 |
