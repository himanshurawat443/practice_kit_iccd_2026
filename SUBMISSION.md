# Submission format

A submission is one archive (`.tar.gz` or `.zip`) per design, named `<handle>-<design>-<n>.tar.gz`, for example `team-a-practice-1-3.tar.gz`.

```
rtl/          your modified design: same files as rtl/filelist.f, same top module and ports
tests/        your cocotb tests: test_*.py plus any helper modules
assertions/   SystemVerilog assertions in *.sv files, attached with bind statements
report.md     one page: deviations found, changes made, how you verified them
trajectory/   optional: your agent's transcript or log
```

A single wrapping folder (for example `team-a-practice-1-3/rtl/...`) is accepted. Other top-level entries are ignored. Each `worked-example/` folder in this kit is a complete submission in this format.

To package and check a submission locally:

```
tar czf runs/sub.tar.gz -C my-submission rtl tests assertions report.md
```

Then run the flow image on it as shown in `flow-image/README.md`.

## Rules on content

- Do not change the top module name, its ports, `rtl/filelist.f` or `flow/constraint.sdc`.
- The design must be synthesizable. No `initial` blocks, delays, `fork`, `force`, DPI, classes, or file and plusarg system tasks in `rtl/`.
- Your tests are cocotb 2.0 modules named `test_*.py`. Copy any public tests you want counted into `tests/`.
- Your tests and assertions must pass on your own design, with assertions enabled, within ten minutes in the flow image. A submission whose own evidence fails is rejected and does not count toward your submission limit.
- Assertions use the SystemVerilog subset that Verilator 5.048 supports; `flow-image/README.md` lists it.

## What the backend runs

| Stage | Checks | Result |
|---|---|---|
| 1. Build | Archive is safe; filelist, ports and constraint unchanged; synthesizable constructs only; Verilator lint; Yosys elaboration | Reject with the reason |
| 2. Public checks | Your design with your assertions; the public testbench (reported only); your tests | Reject if your evidence fails on your own design |
| 3. Hidden checks | Hidden regression tests and formal properties (competition server only) | C |
| 4. Mutation | 40 mutants of your RTL from a fixed operator set, seeded from your archive; your tests and assertions run on each | V, surviving mutants by file and line |
| 5. Synthesis and place and route | Yosys and OpenROAD on sky130hd with the released constraint | P, with area, timing, power and wirelength per stage |

The report lists failed hidden checks by name only, and surviving mutants by file and line only.

## Scored window limits

During the scored window (November 7 to 16, 2026), each team may submit one archive per design every 30 minutes, up to 30 per design. Your design score is your best submission on that design; your total is the sum over the three designs. All valid submissions and their reports are published as an open dataset under CC BY 4.0 after the event.
