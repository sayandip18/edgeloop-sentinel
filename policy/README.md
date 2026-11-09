# policy

Phase 5. The independent validation gate and the risk engine. See [docs/phase5.md](../docs/phase5.md).

| File | Role |
| --- | --- |
| `gate.py` | verdict on a proposed change (scope, blast radius, kustomize, kubeconform, kube-linter, workload policy, optional sandbox). Imports nothing from the author side. |
| `rules.yaml` | the rules, as data |
| `risk.py`, `autonomy.yaml` | maps a gated change to an autonomy level; every class starts at L1 |
| `sandbox.py` | reproduce-then-fix in a throwaway vcluster |
| `test_gate.py` | the bad-patch matrix |

    python policy/gate.py check --base-ref origin/main --head BRANCH
    python policy/test_gate.py
