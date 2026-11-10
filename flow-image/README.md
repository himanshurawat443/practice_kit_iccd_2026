# Proving Ground flow image

The flow image runs the same pipeline as the competition backend (the Flow), on your machine, on a practice or challenge design. Use it to check that your submission builds, that your evidence passes, how many mutants your tests kill, and what your design does to power, performance and area, before you submit.

The image contains Verilator 5.048, cocotb 2.0.1, Yosys 0.68 (with the slang SystemVerilog frontend), SymbiYosys with yices, boolector and z3, OpenROAD with the sky130hd platform, and the `pgflow` runner.

Locally the image runs every stage except the hidden checks (stage 3), which exist only on the competition server. Your local report therefore shows V and P, and a projected score that assumes every hidden check passes (C = 1).

## 1. Get the image

Option A. Load the prebuilt image (download `proving-ground-flow-public-0.1.0.tar.gz`, about 1.8 GB, from the same folder as this kit):

```
docker load -i proving-ground-flow-public-0.1.0.tar.gz
docker run --rm proving-ground/flow:public --help
```

Option B. Build it from this directory (about 30 to 60 minutes; it starts from the public `openroad/orfs` image):

```
docker build -f docker/Dockerfile.public --target public -t proving-ground/flow:public .
```

## 2. Layout

A released design directory:

```
<design>/
  design.yaml            top module, clock, reset, clock period, filelist
  spec.md                the specification
  change_request.md      the change you must implement
  rtl/                   baseline RTL, rtl/filelist.f
  tb/public/             public cocotb testbench (test_*.py)
  flow/constraint.sdc    the fixed clock constraint
```

Your submission is one archive (`.tar.gz` or `.zip`):

```
rtl/          the modified design: same filelist, same top-level ports
tests/        your cocotb tests: test_*.py plus any helper modules
assertions/   SystemVerilog assertions with bind statements (*.sv)
report.md     one page: deviations found, changes made, how verified
trajectory/   optional: your agent's transcript or log
```

A single wrapping folder (for example `handle-practice-1-3/rtl/...`) is accepted. Other top-level entries are ignored.

## 3. Run

```
mkdir -p runs
tar czf runs/sub.tar.gz -C my-submission rtl tests assertions report.md

docker run --rm --cpus 4 -u $(id -u):$(id -g) -e HOME=/tmp \
  -v /path/to/practice-1:/design:ro \
  -v $PWD/runs:/runs \
  proving-ground/flow:public \
  run --design /design --submission /runs/sub.tar.gz --out /runs/out --jobs 4 --cache /runs/cache
```

Results:

```
runs/out/report.md      human-readable report
runs/out/report.json    the same, machine-readable
runs/out/logs/          build, lint, testbench, evidence, mutation and place-and-route logs
```

The first run on a design also places and routes the baseline (a few minutes) and caches it in `--cache`; later runs reuse it. Run `pgflow baseline --design /design --cache /runs/cache` to do this ahead of time.

Useful options: `--skip ppa` (iterate on tests quickly), `--skip mutation`, `--keep-work` (keep the build and simulation directories under `out/work/`).

## 4. What each stage checks

| Stage | Checks | Outcome |
|---|---|---|
| 1. Build | archive is safe; filelist identical to the released one; no clock constraint change; top-level ports identical; no `initial`, delays, `fork`, `force`, DPI, classes, or file and plusarg system tasks in `rtl/`; Verilator lint; Yosys elaboration | reject with the reason |
| 2. Public checks | your design built with your assertions (`verilator --assert`); the public testbench (reported, not required); your tests (all must pass and no assertion may fire) | reject if your evidence fails on your own design |
| 3. Hidden checks | server only | C |
| 4. Mutation | 40 mutants of your RTL; your tests and assertions run on each | V, surviving mutants by file:line |
| 5. Synthesis and place and route | Yosys + OpenROAD on sky130hd with the released constraint | P, area, slack, power, wirelength per stage |

The formulas for C, V and P are in `SCORING.md`.

## 5. Writing evidence that the Flow can run

- Tests are cocotb 2.0 modules named `test_*.py` in `tests/`; helper modules next to them are importable. They drive the top-level module named in `design.yaml`. Copy any public tests you want counted into `tests/`.
- The Flow builds with the design's Verilator options plus `--assert`. Registers and inputs start at zero, and test randomization is seeded from your archive hash, so runs are reproducible. Do not rely on power-up values: reset the design explicitly, and check reset behavior after activity.
- Your tests must finish within 10 minutes on your own design. Each mutant gets twice your run time plus 15 seconds (30 to 300 seconds).
- Assertions are SystemVerilog modules in `assertions/` attached with `bind`, for example:

```systemverilog
module my_checks(input logic clk, input logic rst_n, input logic [7:0] count, input logic wrap);
  a_wrap: assert property (@(posedge clk) disable iff (!rst_n) wrap |-> count == 8'd0)
    else $error("wrap without zero");
endmodule
bind my_counter my_checks u_checks (.*);
```

Verilator 5.048 support for concurrent assertions, as checked in this image:

| Supported | Not supported |
|---|---|
| `|->`, `|=>`, `##N`, `##[m:n]`, `##[m:$]`, `disable iff`, named sequences and properties | `s_eventually`, `eventually [m:n]`, `always`, `nexttime`, `strong(...)` |
| `$rose`, `$fell`, `$stable`, `$past` (with depth), `$onehot`, `$onehot0`, `$countones` | `not` applied to a sequence |
| repetition `[*n]`, `[*m:n]`, goto `[->n]`, non-consecutive `[=n]`, `throughout`, `intersect`, `first_match`, `until`, `and`, `or`, `implies`, `iff` | `$changed` after a delay in a sequence (write `b != $past(b)` instead) |
| `assert property`, `cover property`, immediate `assert` | |

A failed assertion with no action block, or with `$error`, stops the simulation and fails the running test. Keep assertions to synthesizable signals of the bound module.

## 6. Reading the report

- A rejected submission scores 0 and does not count toward your limit. The reason and a log tail are in the report.
- Surviving mutants are listed as `rtl/<file>:<line>`: write a test or assertion that fails if that line is changed.
- P is 0.5 when your design matches the baseline; see the per-stage table for where area or timing moved. A design that does not meet timing after routing has P capped at 0.3.

Results are published as an open dataset under CC BY 4.0 after the event.
