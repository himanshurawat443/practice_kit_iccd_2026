# Proving Ground: Agentic AI for Design Verification Challenge, ICCD 2026

## Practice kit

This kit contains everything you need to learn the task before the scored window opens: two practice designs, a worked example submission for each, and the flow image that checks a submission on your own machine.

Practice designs are not scored, and practice submissions are not uploaded. You check them locally with the flow image, which runs the same stages as the competition backend except the hidden checks.

## Contents

| Path | Content |
|---|---|
| `practice-1/` | A streaming CRC unit (datapath block). Specification, baseline RTL, public testbench, change request, clock constraint, worked example. |
| `practice-2/` | A four-channel PWM timer with an APB register interface (control block). Same contents. |
| `flow-image/` | The flow image: how to load or build it, how to run it, and how each stage is scored (`SCORING.md`). |
| `SUBMISSION.md` | The submission format, the rules on content, and how scoring works. |

The prebuilt flow image is a separate download in the same folder as this kit: `proving-ground-flow-public-0.1.0.tar.gz` (about 1.8 GB). You can also build it yourself from `flow-image/`.

## The task, in short

Each design comes with a specification, a public testbench and a change request. The baseline RTL contains a few deviations from the specification that the public testbench does not catch.

1. Find the deviations and fix them.
2. Implement the change request.
3. Deliver evidence: your own cocotb tests and SystemVerilog assertions that show the design is correct.
4. Keep or improve power, performance and area (PPA) under the released clock constraint.

For the practice designs, the deviations are disclosed in `worked-example/report.md`. Try to find them yourself first.

## Getting started

1. Read `practice-1/README.md`, then `practice-1/spec.md` and `practice-1/change_request.md`.
2. Run the public testbench: `make -C practice-1/tb/public` (needs Verilator 5.036 or later and cocotb 2.0, or use the flow image).
3. Load the flow image and run it on the worked example, as described in `flow-image/README.md`. Compare your report with `practice-1/worked-example/flow-report.md`.
4. Build your own submission for `practice-1` and `practice-2` and run the flow image on it until the report is clean.

## Score

Score = 100 × C × (0.5 × V + 0.5 × P)

| Term | Meaning | Range |
|---|---|---|
| C | Correctness: fraction of hidden regression tests and hidden formal properties that pass, including those for the change request | 0 to 1 |
| V | Verification quality: fraction of 40 injected faults (mutants) that your own tests and assertions detect | 0 to 1 |
| P | PPA after synthesis and place and route on sky130hd, compared with the baseline. 0.5 equals the baseline; a design that fails timing is capped at 0.3 | 0 to 1 |

Locally the flow image reports V, P and a projected score that assumes C = 1. C is measured only on the competition server, during the scored window.

## Dates

| Date | Event |
|---|---|
| Now | Practice kit available |
| November 7, 2026, 09:00 HKT (November 6, 5:00 PM PST) | Scored window opens; Design 1 released |
| November 10, 2026 | Design 2 released; registration closes at 23:59 HKT |
| November 13, 2026 | Design 3 released |
| November 16, 2026, 23:59 HKT (7:59 AM PST) | Scored window closes for all three designs |
| November 17, 2026 | Results published |
| November 18, 2026 | Awards at the ICCD 2026 closing session, Hong Kong |

How to submit during the scored window is announced on the competition page before November 7.

## Questions

Post questions on the competition page or write to [contact address].
