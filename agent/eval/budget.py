"""Admission reservations for every provider attempt, including retries and tools.

The binding supplies conservative input/output ceilings BEFORE invoking a model.
An unreported/failed call keeps its reservation, rather than spending for free.
Thought tokens are part of the output ceiling and charged at the output price.
"""
from __future__ import annotations

import math
from threading import RLock


class BudgetReached(ValueError):
    """No next provider call may start."""


class EvalBudget:
    def __init__(self, max_calls, max_tokens, max_cost_usd, prices):
        for name, value in dict(max_calls=max_calls, max_tokens=max_tokens,
                                max_cost_usd=max_cost_usd, **prices).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if type(max_calls) is not int or type(max_tokens) is not int or set(prices) != {"input", "output"}:
            raise ValueError("Integer call/token caps and input/output prices are required")
        self.caps = dict(max_calls=max_calls, max_tokens=max_tokens, max_cost_usd=max_cost_usd, prices=dict(prices))
        self._totals = dict(calls=0, tokens=0, cost_usd=0.0)
        self._pending = []
        self._lock = RLock()
        self.checkpoint = lambda totals: None

    def _cost(self, inputs, outputs):
        return (inputs * self.caps["prices"]["input"] + outputs * self.caps["prices"]["output"]) / 1_000_000

    @staticmethod
    def _tokens(value):
        if type(value) is not int or value < 0:
            raise ValueError("Every call requires nonnegative integer token ceilings")
        return value

    def allow(self, *, input_tokens, output_tokens):
        inputs, outputs = self._tokens(input_tokens), self._tokens(output_tokens)
        with self._lock:
            cost = self._cost(inputs, outputs)
            proposed = dict(calls=self._totals["calls"] + 1, tokens=self._totals["tokens"] + inputs + outputs,
                            cost_usd=self._totals["cost_usd"] + cost)
            if any(proposed[k] > self.caps["max_" + k] for k in proposed):
                raise BudgetReached("Evaluation budget reached before the next model call")
            self._totals = proposed
            self._pending.append((inputs, outputs))
            # Persist the reservation before the provider can spend it (also on a crash).
            self.checkpoint(self.totals())

    def charge(self, usage, calls=1):
        with self._lock:
            if type(calls) is not int or calls < 1 or calls > len(self._pending):
                raise ValueError("Model calls must be admitted before charging")
            reservations = self._pending[:calls]
            inputs = sum(r[0] for r in reservations)
            outputs = sum(r[1] for r in reservations)
            counts = [usage.get(k) for k in ("input", "output", "thoughts")]
            exceeded = False
            if all(v is not None for v in counts):
                actual_input, actual_output, thoughts = map(self._tokens, counts)
                exceeded = actual_input > inputs or actual_output + thoughts > outputs
                self._totals["tokens"] += actual_input + actual_output + thoughts - inputs - outputs
                self._totals["cost_usd"] += self._cost(actual_input, actual_output + thoughts) - self._cost(inputs, outputs)
            else:
                self._totals["usage_incomplete"] = True
            del self._pending[:calls]
            self.checkpoint(self.totals())
            if exceeded:
                raise ValueError("Reported usage exceeds the binding's admitted token ceiling")

    def totals(self):
        with self._lock:
            return dict(self._totals)

    def restore(self, totals):
        """Unsettled calls from an interrupted process remain conservatively spent."""
        with self._lock:
            if self._totals["calls"] or self._pending:
                raise ValueError("Resume requires a fresh budget")
            self._totals.update(totals)

    def exhausted(self):
        return any(self._totals[k] >= self.caps["max_" + k] for k in ("calls", "tokens", "cost_usd"))
