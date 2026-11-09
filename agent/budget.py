"""Per-run LLM budget (Phase 6). Shared across processes through an append-only JSONL ledger, because the eval harness
starts a fresh responder process per scenario and the diagnosis loop is a separate daemon.

A "run" is a label (env EDGELOOP_LLM_RUN_ID, default "default"). Every model call appends its token usage; before each
call the agent asks `exhausted()` and escalates instead of calling when a cap is hit.

Env:
  EDGELOOP_LLM_RUN_ID            run label (the live verifier sets a fresh one per run)
  EDGELOOP_LLM_RUN_TOKENS        total-token cap for the run (default 500000)
  EDGELOOP_LLM_RUN_USD           optional dollar cap; REQUIRES both prices below, otherwise the budget refuses to start
  EDGELOOP_LLM_PRICE_IN_PER_M    USD per million input tokens   (look up the current price for your model)
  EDGELOOP_LLM_PRICE_OUT_PER_M   USD per million output tokens  (reasoning tokens bill as output and are included in
                                 completion_tokens)
  EDGELOOP_LLM_LEDGER            ledger path (default agent/state/llm_usage.jsonl)
Prices are deliberately not hardcoded: they change, and a wrong built-in number would give false confidence.
"""
import json
import os
import time
from pathlib import Path

DEFAULT_TOKENS = 500_000
DEFAULT_LEDGER = Path(__file__).resolve().parent / "state" / "llm_usage.jsonl"


class BudgetConfigError(ValueError):
    pass


class Budget:
    def __init__(self, path=None, run_id="default", max_tokens=DEFAULT_TOKENS, max_usd=None, price_in=None, price_out=None):
        if max_usd is not None and (price_in is None or price_out is None):
            raise BudgetConfigError("a dollar cap needs EDGELOOP_LLM_PRICE_IN_PER_M and EDGELOOP_LLM_PRICE_OUT_PER_M")
        self.path, self.run_id = Path(path or DEFAULT_LEDGER), run_id
        self.max_tokens, self.max_usd, self.price_in, self.price_out = max_tokens, max_usd, price_in, price_out

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        f = lambda k: float(e[k]) if e.get(k) else None
        return cls(e.get("EDGELOOP_LLM_LEDGER"), e.get("EDGELOOP_LLM_RUN_ID", "default"),
                   int(e.get("EDGELOOP_LLM_RUN_TOKENS", DEFAULT_TOKENS)), f("EDGELOOP_LLM_RUN_USD"),
                   f("EDGELOOP_LLM_PRICE_IN_PER_M"), f("EDGELOOP_LLM_PRICE_OUT_PER_M"))

    def _rows(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue   # a half-written last line while another process appends
            if row.get("run") == self.run_id:
                out.append(row)
        return out

    def spent(self):
        rows = self._rows()
        p, c = sum(r.get("prompt", 0) for r in rows), sum(r.get("completion", 0) for r in rows)
        usd = None if self.price_in is None else round(p / 1e6 * self.price_in + c / 1e6 * self.price_out, 4)
        return {"calls": len(rows), "prompt": p, "completion": c, "tokens": p + c, "usd": usd}

    def exhausted(self):
        """None while within budget, else a human-readable reason."""
        s = self.spent()
        if s["tokens"] >= self.max_tokens:
            return f"run token budget exhausted ({s['tokens']} >= {self.max_tokens}, run {self.run_id!r})"
        if self.max_usd is not None and s["usd"] is not None and s["usd"] >= self.max_usd:
            return f"run dollar budget exhausted (${s['usd']} >= ${self.max_usd}, run {self.run_id!r})"
        return None

    def charge(self, prompt, completion):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "run": self.run_id, "prompt": int(prompt or 0), "completion": int(completion or 0)}) + "\n")
