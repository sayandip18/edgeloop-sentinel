# evals

Failure catalog and runner. See [docs/phase2.md](../docs/phase2.md).

    bash scripts/eval.sh list
    bash scripts/eval.sh run oom-kill            # inject via Git, score, revert
    bash scripts/eval.sh run all --dry-run       # show the plan, change nothing

`scenarios/*.yaml` one file per failure, `run.py` the runner, `scoring.py` pure scoring (unit-tested in `test_scoring.py`), `results/` JSON output (gitignored).
