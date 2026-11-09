# evals

Failure catalog and runner. See [docs/phase2.md](../docs/phase2.md).

    bash scripts/eval.sh list
    bash scripts/eval.sh run oom-kill            # inject via Git, score, revert
    bash scripts/eval.sh run all --dry-run       # show the plan, change nothing

13 scenarios (`novel: true` and `adversarial: true` mark the Phase 6 ones). `--responder CMD` plugs in a diagnoser: `python evals/run.py run all --detector --responder "python agent/responder.py"`.

`scenarios/*.yaml` one file per failure, `run.py` the runner, `scoring.py` pure scoring (unit-tested in `test_scoring.py`), `results/` JSON output (gitignored).
