#!/usr/bin/env python3
"""
agent_treasury — policy-gated spend authority for an autonomous agent.

Design principle: the agent NEVER holds an unbounded key. Every spend is
evaluated against a declarative policy, logged, and either auto-approved
(below threshold), queued for human approval (above threshold), or refused
(hard limits). This is what makes autonomy defensible rather than reckless.

Ledger is append-only JSONL so the audit trail survives crashes.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Literal

SOL_BIN = Path.home() / ".local/share/solana/install/active_release/bin"
RPC = "http://127.0.0.1:8899"

Decision = Literal["auto_approved", "queued_for_human", "refused"]


@dataclass
class Policy:
    """Spend authority limits. All values in SOL."""
    per_tx_max: float = 1.0            # largest single spend without human sign-off
    daily_max: float = 5.0             # rolling 24h ceiling
    hard_ceiling: float = 20.0         # absolute cap, human approval cannot bypass in one tx
    allowlist: list[str] = field(default_factory=list)  # empty = any recipient
    require_human_above: float = 0.5   # threshold at which a human must approve

    def validate(self) -> None:
        assert self.per_tx_max <= self.daily_max <= self.hard_ceiling, \
            "policy must be monotonic: per_tx_max <= daily_max <= hard_ceiling"
        assert self.require_human_above <= self.per_tx_max, \
            "require_human_above must not exceed per_tx_max"


class AgentTreasury:
    def __init__(self, workdir: Path, policy: Policy | None = None):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy()
        self.policy.validate()
        self.ledger = self.workdir / "ledger.jsonl"
        self.pending = self.workdir / "pending_approval.jsonl"
        self.env = {**os.environ,
                    "PATH": f"{SOL_BIN}:{os.environ.get('PATH', '')}"}

    # ---------- introspection ----------
    def _sh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True,
                              env=self.env, timeout=120)

    def address(self) -> str:
        return self._sh("solana", "address").stdout.strip()

    def balance(self) -> float:
        out = self._sh("solana", "balance", "--url", RPC).stdout.strip()
        try:
            return float(out.split()[0])
        except (IndexError, ValueError):
            return 0.0

    def spent_24h(self) -> float:
        if not self.ledger.exists():
            return 0.0
        cutoff = time.time() - 86400
        total = 0.0
        for line in self.ledger.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("ts", 0) >= cutoff and rec.get("status") == "executed":
                total += float(rec.get("amount_sol", 0))
        return total

    # ---------- policy engine ----------
    def evaluate(self, recipient: str, amount_sol: float) -> tuple[Decision, str]:
        p = self.policy
        if amount_sol <= 0:
            return "refused", "amount must be positive"
        if amount_sol > p.hard_ceiling:
            return "refused", f"{amount_sol} SOL exceeds hard ceiling {p.hard_ceiling}"
        if p.allowlist and recipient not in p.allowlist:
            return "refused", f"recipient {recipient} not on allowlist"
        if amount_sol > p.per_tx_max:
            return "refused", f"{amount_sol} SOL exceeds per-tx max {p.per_tx_max}"
        if self.spent_24h() + amount_sol > p.daily_max:
            return "refused", (f"would exceed 24h budget "
                               f"({self.spent_24h():.3f}+{amount_sol} > {p.daily_max})")
        if amount_sol > p.require_human_above:
            return "queued_for_human", f"{amount_sol} SOL above human-approval threshold"
        if self.balance() < amount_sol:
            return "refused", f"insufficient balance ({self.balance()} SOL)"
        return "auto_approved", "within autonomous authority"

    def _log(self, path: Path, **rec) -> None:
        rec.setdefault("ts", time.time())
        with path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    # ---------- execution ----------
    def spend(self, recipient: str, amount_sol: float, memo: str = "") -> dict:
        decision, reason = self.evaluate(recipient, amount_sol)
        rec = {"recipient": recipient, "amount_sol": amount_sol,
               "memo": memo, "status": decision, "reason": reason,
               "balance_before": self.balance()}

        if decision == "refused":
            self._log(self.ledger, **rec)
            return rec

        if decision == "queued_for_human":
            self._log(self.pending, **rec)
            self._log(self.ledger, **rec)
            return rec

        # auto_approved -> execute for real
        res = self._sh("solana", "transfer", recipient, str(amount_sol),
                       "--url", RPC, "--allow-unfunded-recipient",
                       "--fee-payer", str(Path.home() / ".config/solana/id.json"))
        rec["signature"] = None
        for tok in res.stdout.split():
            if len(tok) > 60 and tok.isalnum():
                rec["signature"] = tok
        rec["status"] = "executed" if res.returncode == 0 else "failed"
        rec["stderr"] = res.stderr.strip()[:300] or None
        rec["balance_after"] = self.balance()
        self._log(self.ledger, **rec)
        return rec

    def approve_pending(self, index: int) -> dict:
        """Human gate: release a queued spend."""
        lines = [l for l in self.pending.read_text().splitlines() if l.strip()]
        if not lines or index >= len(lines):
            return {"error": "nothing pending"}
        rec = json.loads(lines[index])
        res = self._sh("solana", "transfer", rec["recipient"],
                       str(rec["amount_sol"]), "--url", RPC,
                       "--allow-unfunded-recipient")
        rec["human_approved"] = True
        rec["status"] = "executed" if res.returncode == 0 else "failed"
        # remove the approved entry from the pending file
        remaining = [l for i, l in enumerate(lines) if i != index]
        self.pending.write_text("\n".join(remaining) + ("\n" if remaining else ""))
        self._log(self.ledger, **rec)
        return rec


if __name__ == "__main__":
    t = AgentTreasury(Path(__file__).parent)
    print("wallet:  ", t.address())
    print("balance: ", t.balance(), "SOL")
    print("spent24h:", t.spent_24h(), "SOL")
    print("policy:  ", asdict(t.policy))
