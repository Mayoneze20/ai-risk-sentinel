#!/usr/bin/env python3
"""
AI Risk Sentinel — off-chain model service (Phase 1: scoring core + self-test).

The service watches AIOracle `ObservationRecorded` events (Base Sepolia), keeps a
rolling window of swaps from the watched Uniswap V3 pool, computes
manipulation-like features, scores them with a transparent heuristic model, and
(Phase 2) signs the decision and submits it to AIOracle.submitDecision.

The AI genuinely decides: no on-chain decision exists unless this service
computes features, scores them, and signs the result. The contract-side
guardrails in GuardianVault (confidence threshold, cooldown, caps) then decide
how much of that decision the vault is allowed to act on.

Features (each normalized to 0..1 over the current window):
  price_impact   Two sub-signals, max taken: (a) largest single-swap |tick
                 move| vs the window median |tick move| (jumpiness), and
                 (b) net one-way tick drift across the window (a pump where
                 every swap jumps equally is invisible to (a) but not (b)).
  volume_spike   Window |amount0| volume vs the rolling median window volume.
  concentration  Top sender's share of window volume. One wallet dominating is
                 the classic wash / manipulation signature.
  roundtrip      Share of volume from senders who both bought AND sold inside
                 the window. Wash trading round-trips; real flow mostly doesn't.

Scoring (documented weights, sum = 1.0):
  risk = 100 * (0.30*price_impact + 0.25*volume_spike
              + 0.25*concentration + 0.20*roundtrip)
  action: risk >= 75 -> "exit" | 50..74 -> "pause" | < 50 -> "hold"

Confidence (0..100): grows with the number of swaps in the window and with the
distance of the risk score from the nearest action boundary — a risk of 51 is a
coin-flip Pause, a risk of 95 is a sure Exit. GuardianVault's minConfidence
guardrail consumes exactly this number.

featuresHash = SHA-256 of the canonical JSON of the feature vector. The full
breakdown stays in the service log; its fingerprint goes on-chain.

Stdlib only. Run the offline self-test with:  python3 model_service.py --selftest
"""

import hashlib
import json
import math
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_SEPOLIA_RPC = "https://sepolia.base.org"
BASE_SEPOLIA_CHAIN_ID = 84532

# Watched pool: Uniswap V3 WETH/USDC 0.3% on Base Sepolia, verified on-chain
# via the official factory getPool() on 2026-11-24 (see BUILD.md).
POOL_ADDRESS = "0x46880b404CD35c165EDdefF7421019F8dD25F4Ad"

WINDOW_MAX_SWAPS = 30          # rolling window size (swaps)
WINDOW_MAX_SECONDS = 600       # ... or 10 minutes, whichever fills first

WEIGHTS = {
    "price_impact": 0.30,
    "volume_spike": 0.25,
    "concentration": 0.25,
    "roundtrip": 0.20,
}

EXIT_THRESHOLD = 75.0
PAUSE_THRESHOLD = 50.0

ACTION_TO_UINT = {"hold": 0, "pause": 1, "exit": 2}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Swap:
    sender: str
    amount0: int          # signed, raw pool units (token0 = WETH here)
    amount1: int          # signed, raw pool units (token1 = USDC here)
    tick: int             # pool tick AFTER this swap
    prev_tick: int        # pool tick BEFORE this swap
    timestamp: float      # seconds (block time in live mode, synthetic in tests)

    @property
    def volume(self) -> int:
        return abs(self.amount0)

    @property
    def tick_move(self) -> int:
        return abs(self.tick - self.prev_tick)

    @property
    def direction(self) -> int:
        # +1 = token0 flowed out of the pool to the swapper side convention:
        # amount0 < 0 means the swapper received token0 (a "buy" of token0).
        if self.amount0 < 0:
            return 1
        if self.amount0 > 0:
            return -1
        return 0


@dataclass
class Decision:
    action: str           # "hold" | "pause" | "exit"
    confidence: int       # 0..100
    risk: float           # 0..100
    features: dict        # normalized feature vector
    features_hash: str    # sha256 hex of canonical feature JSON

    def digest_payload(self, oracle_address: str, observation_id: int) -> dict:
        """The fields the on-chain decisionDigest() commits to (Phase 2 signing)."""
        return {
            "tag": "AI-RISK-SENTINEL-DECISION",
            "oracle": oracle_address.lower(),
            "observationId": observation_id,
            "action": ACTION_TO_UINT[self.action],
            "confidence": self.confidence,
            "featuresHash": self.features_hash,
        }


# --------------------------------------------------------------------------
# Feature extraction + scoring
# --------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_features(window: list[Swap], baseline_volume: float | None) -> dict:
    """Compute the normalized feature vector for a window of swaps."""
    n = len(window)
    if n == 0:
        return {k: 0.0 for k in WEIGHTS}

    # --- price impact: jumpiness vs typical moves, OR one-way drift --------
    # Two sub-signals, take the max:
    #  (a) jump: largest single-swap |tick move| vs the window median move
    #      (a jump 8x the typical move saturates);
    #  (b) drift: net one-way tick displacement over the window (a sustained
    #      200-tick ~2% one-way drift saturates). A pump where EVERY swap
    #      jumps equally is invisible to (a) but fully visible to (b).
    moves = [s.tick_move for s in window]
    median_move = statistics.median(moves) if moves else 0
    max_move = max(moves) if moves else 0
    if median_move > 0:
        jump = _clamp01((max_move / median_move) / 8.0)
    else:
        jump = _clamp01(max_move / 40.0)  # any move vs flat baseline
    net_drift = abs(window[-1].tick - window[0].prev_tick) if window else 0
    drift = _clamp01(net_drift / 200.0)
    price_impact = max(jump, drift)

    # --- volume spike: window volume vs rolling baseline -------------------
    volume = sum(s.volume for s in window)
    if baseline_volume and baseline_volume > 0:
        # 4x the baseline window volume saturates the feature.
        volume_spike = _clamp01((volume / baseline_volume) / 4.0)
    else:
        volume_spike = 0.0

    # --- concentration: top sender share of volume --------------------------
    by_sender: dict[str, int] = {}
    for s in window:
        by_sender[s.sender] = by_sender.get(s.sender, 0) + s.volume
    concentration = (max(by_sender.values()) / volume) if volume > 0 else 0.0

    # --- roundtrip: volume share from senders who both bought and sold -----
    directions: dict[str, set] = {}
    for s in window:
        directions.setdefault(s.sender, set()).add(s.direction)
    rt_volume = sum(
        by_sender[addr] for addr, dirs in directions.items() if 1 in dirs and -1 in dirs
    )
    roundtrip = (rt_volume / volume) if volume > 0 else 0.0

    return {
        "price_impact": round(_clamp01(price_impact), 6),
        "volume_spike": round(_clamp01(volume_spike), 6),
        "concentration": round(_clamp01(concentration), 6),
        "roundtrip": round(_clamp01(roundtrip), 6),
    }


def score(features: dict) -> float:
    return 100.0 * sum(WEIGHTS[k] * features[k] for k in WEIGHTS)


def decide(window: list[Swap], baseline_volume: float | None = None) -> Decision:
    features = compute_features(window, baseline_volume)
    risk = score(features)

    if risk >= EXIT_THRESHOLD:
        action = "exit"
    elif risk >= PAUSE_THRESHOLD:
        action = "pause"
    else:
        action = "hold"

    # Confidence: sample sufficiency x distance from nearest boundary.
    sample_factor = min(1.0, len(window) / 20.0)
    boundaries = [0.0, PAUSE_THRESHOLD, EXIT_THRESHOLD, 100.0]
    distance = min(abs(risk - b) for b in boundaries)
    distance_factor = min(1.0, distance / 25.0)
    confidence = int(round(100 * sample_factor * (0.35 + 0.65 * distance_factor)))
    confidence = max(0, min(100, confidence))

    canonical = json.dumps(features, sort_keys=True, separators=(",", ":"))
    features_hash = hashlib.sha256(canonical.encode()).hexdigest()

    return Decision(
        action=action,
        confidence=confidence,
        risk=round(risk, 2),
        features=features,
        features_hash=features_hash,
    )


# --------------------------------------------------------------------------
# Rolling window tracker (used by the live poller in Phase 2)
# --------------------------------------------------------------------------

@dataclass
class WindowTracker:
    swaps: list[Swap] = field(default_factory=list)
    past_window_volumes: list[float] = field(default_factory=list)

    def add(self, swap: Swap) -> Decision | None:
        self.swaps.append(swap)
        now = swap.timestamp
        self.swaps = [
            s for s in self.swaps if now - s.timestamp <= WINDOW_MAX_SECONDS
        ][-WINDOW_MAX_SWAPS:]
        if len(self.swaps) < 5:
            return None  # not enough evidence to judge yet
        baseline = (
            statistics.median(self.past_window_volumes)
            if self.past_window_volumes
            else None
        )
        return decide(self.swaps, baseline)

    def roll_baseline(self) -> None:
        """Snapshot the current window volume into the baseline history."""
        if self.swaps:
            self.past_window_volumes.append(sum(s.volume for s in self.swaps))
            self.past_window_volumes = self.past_window_volumes[-24:]


# --------------------------------------------------------------------------
# Live polling (Phase 2 wiring — implemented, not exercised in Phase 1)
# --------------------------------------------------------------------------

def rpc_call(method: str, params: list) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        BASE_SEPOLIA_RPC, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    if "error" in out:
        raise RuntimeError(f"RPC error: {out['error']}")
    return out["result"]


# ObservationRecorded(uint256,address,address,int256,int256,int24,uint256)
OBSERVATION_TOPIC = None  # Phase 2: keccak of the event signature, filled at wiring time.


def poll_observations(oracle_address: str, from_block: int) -> list[dict]:
    """Fetch ObservationRecorded logs from the AIOracle (Phase 2)."""
    if OBSERVATION_TOPIC is None:
        raise NotImplementedError(
            "Phase 2: set OBSERVATION_TOPIC to keccak256 of the "
            "ObservationRecorded signature before live polling."
        )
    return rpc_call(
        "eth_getLogs",
        [{
            "fromBlock": hex(from_block),
            "toBlock": "latest",
            "address": oracle_address,
            "topics": [OBSERVATION_TOPIC],
        }],
    )


# --------------------------------------------------------------------------
# Self-test (Phase 1 verification — synthetic swap sequences)
# --------------------------------------------------------------------------

def _mk(sender: str, amount0: int, tick_from: int, tick_to: int, t: float) -> Swap:
    return Swap(
        sender=sender,
        amount0=amount0,
        amount1=-amount0 * 3000,  # nominal counter-flow, value unused by features
        tick=tick_to,
        prev_tick=tick_from,
        timestamp=t,
    )


def case_normal_flow() -> list[Swap]:
    """30 swaps, 10 distinct senders, small sizes, alternating directions,
    tiny tick moves. Organic flow -> expect hold."""
    swaps, tick, t = [], 0, 0.0
    senders = [f"0xuser{i}" for i in range(10)]
    for i in range(30):
        direction = 1 if i % 2 == 0 else -1
        amount = direction * (10**15 + (i % 5) * 10**14)
        new_tick = tick + (1 if i % 3 == 0 else -1)
        swaps.append(_mk(senders[i % 10], amount, tick, new_tick, t))
        tick, t = new_tick, t + 20.0
    return swaps


def case_wash_burst() -> list[Swap]:
    """One sender, 24 rapid alternating large swaps. Wash trading signature:
    concentration 1.0, roundtrip 1.0 -> expect pause or exit."""
    swaps, tick, t = [], 0, 0.0
    for i in range(24):
        direction = 1 if i % 2 == 0 else -1
        amount = direction * 5 * 10**16
        new_tick = tick + direction * 2
        swaps.append(_mk("0xwasher", amount, tick, new_tick, t))
        tick, t = new_tick, t + 3.0
    return swaps


def case_pump() -> list[Swap]:
    """12 same-direction large swaps from 2 senders, price jumping hard.
    Pump signature: price impact + concentration + volume -> pause or exit."""
    swaps, tick, t = [], 0, 0.0
    for i in range(12):
        amount = 8 * 10**16
        new_tick = tick + 25
        swaps.append(_mk(f"0xpump{i % 2}", amount, tick, new_tick, t))
        tick, t = new_tick, t + 8.0
    return swaps


def case_concentrated_whale() -> list[Swap]:
    """One whale = ~90% of volume, one direction, moderate price drift,
    padded with small organic swaps -> expect pause or exit."""
    swaps, tick, t = [], 0, 0.0
    for i in range(10):
        new_tick = tick + 1
        swaps.append(_mk(f"0xuser{i}", 10**15, tick, new_tick, t))
        tick, t = new_tick, t + 15.0
    for i in range(6):
        new_tick = tick + 6
        swaps.append(_mk("0xwhale", 3 * 10**16, tick, new_tick, t))
        tick, t = new_tick, t + 10.0
    return swaps


def run_selftest() -> int:
    # Baseline: a "typical" earlier window volume for spike comparisons.
    baseline_volume = float(sum(s.volume for s in case_normal_flow()))

    cases = [
        ("normal flow (diverse, small, alternating)", case_normal_flow(),
         {"hold"}),
        ("wash burst (1 sender, rapid round-trips)", case_wash_burst(),
         {"pause", "exit"}),
        ("pump (2 senders, one-way, price jumps)", case_pump(),
         {"pause", "exit"}),
        ("concentrated whale (~90% of volume)", case_concentrated_whale(),
         {"pause", "exit"}),
    ]

    failures = 0
    for name, window, expected in cases:
        d = decide(window, baseline_volume)
        ok = d.action in expected
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       action={d.action} (expected one of {sorted(expected)}), "
              f"risk={d.risk}, confidence={d.confidence}")
        print(f"       features={json.dumps(d.features, sort_keys=True)}")
        print(f"       featuresHash=0x{d.features_hash}")
    print(f"\nSelf-test: {len(cases) - failures}/{len(cases)} cases passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    print(__doc__)
    print("Phase 1: run with --selftest. Live polling/submission is Phase 2.")
