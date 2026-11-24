# AI Risk Sentinel — Build Notes (Reactive Omni Buildathon, "Reactive Meets AI" track)

Phase 1 design, written 2026-11-24 (JST). Phase 1 = code + local verification only;
no deploys. Phase 2 = deploys + live loop. Deadline Dec 6 05:00 JST; submission
target Dec 5. Testnets only, zero own money.

## One-line concept

A Reactive Smart Contract on Lasna watches Swap events on a real Uniswap V3 pool on
Base Sepolia and forwards every swap on-chain to an AI oracle contract; an off-chain
model service scores each burst of swap activity for manipulation-like patterns and
posts a signed decision (hold / pause / exit) to the oracle; a guardian vault on Base
Sepolia acts on the decision under hard contract-side guardrails.

The AI is a running part of the system, not a coding aid: no decision exists unless
the model service computes features, scores them, signs the result, and submits it.

## Watched source (verified on-chain, read-only, 2026-11-24)

- DEX: Uniswap V3 on Base Sepolia. Factory (official Uniswap docs, Base Sepolia):
  `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`.
- Pool (verified via factory `getPool(WETH, USDC, 3000)`, read-only `cast call`):
  **WETH/USDC 0.3% pool `0x46880b404CD35c165EDdefF7421019F8dD25F4Ad`**
  - WETH (Base Sepolia): `0x4200000000000000000000000000000000000006`
  - USDC (Base Sepolia): `0x036CbD53842c5426634e7929541eC2318f3dCF7e`
  - `liquidity()` at block 62957669: 692,848,927,048,573 — the deepest of the three
    fee tiers (0.05%: 4.9e11; 1%: 1.3e12).
- Event watched: `Swap(address,address,int256,int256,uint160,uint128,int24)`,
  topic0 `0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67`.
  Indexed: sender (topic_1), recipient (topic_2). Data: amount0, amount1,
  sqrtPriceX96, liquidity, tick.
- Activity caveat (honest): third-party swaps on this testnet pool are sparse —
  0 Swap logs in blocks 62956669–62957669. The Phase 2 live demo therefore drives
  its own swap flow through the official SwapRouter against this same real pool:
  benign swaps for the "hold" leg, then a scripted wash/pump pattern for the
  "pause/exit" leg. The pool, the events, and the reactive loop are all real;
  only the traders are ours.

## Components

### 1. SentinelReactive.sol — Lasna (chain 5318007)
Omni pattern, same shape as the gate's GateReactive:
- Constructor: `SYSTEM.subscribe(84532, POOL, SWAP_TOPIC0, IGNORE, IGNORE, IGNORE)`
  on the system contract `0x8888888888888888888888888888888888888888`.
- `react(LogRecord)` (onlySystem): decodes amount0/amount1/sqrtPriceX96/tick from
  `log.data`, sender from `log.topic_1`; counts observations; requests a callback
  to AIOracle on Base Sepolia with payload
  `recordObservation(address,address,address,int256,int256,uint160,int24,uint256)`.
  First argument is the `address(0)` placeholder — the system contract overwrites
  the first 160 payload bits with this RSC's address (same rule as the gate), so
  the oracle learns *which* RSC reported and can allowlist it.

### 2. AIOracle.sol — Base Sepolia (chain 84532)
The on-chain meeting point of the reactive loop and the AI:
- `recordObservation(...)` — called via Reactive callback. Requires the reported
  RSC address (first arg) to be owner-allowlisted. Stores the observation, emits
  `ObservationRecorded(id, pool, sender, amount0, amount1, tick, originTxHash)`.
- `submitDecision(observationId, action, confidence, featuresHash, signature)` —
  the model service's entry point. `action` ∈ {Hold, Pause, Exit}; `confidence`
  0–100. The signature is an ECDSA `personal_sign` over the decision digest,
  verified on-chain with `ecrecover` against the owner-configured `modelSigner`.
  A decision cannot be forged by anyone who is not the model service.
- `markExecuted(decisionId)` — callable only by the registered GuardianVault, so
  each decision is consumed exactly once.
- Owner can rotate `modelSigner` and the vault registration.

### 3. GuardianVault.sol — Base Sepolia (chain 84532)
A demo vault holding testnet ETH deposits; it is the thing being protected.
- `actOnDecision(decisionId)` — permissionless trigger, strict guardrails:
  - decision exists, not yet executed, and is fresh (≤ `decisionMaxAge`);
  - `confidence >= minConfidence` (default 60) — below that, the decision is
    recorded but not acted on;
  - cooldown: at most one protective action per `cooldownSeconds` (default 300);
  - Hold → marks consumed, no state change;
  - Pause → `paused = true`: owner withdrawals are blocked while paused
    (the protection made visible);
  - Exit → transfers at most `maxExitBpsPerAction` (default 5000 = 50%) of the
    vault balance to `safeAddress`; repeat exits are bounded by the same cap
    and the cooldown.
- Owner override: `setPaused(bool)` (manual unpause/pause), guardrail setters,
  `setSafeAddress`, `emergencyWithdraw` (owner-only, works even while paused —
  the human always has the final word).
- Events for every action so the demo's Execution evidence is a list of tx hashes.

### 4. model_service.py — off-chain (Python, stdlib only)
- Polls AIOracle `ObservationRecorded` events over JSON-RPC (`eth_getLogs`,
  `urllib`) in Phase 2; in Phase 1 the scoring core is exercised offline.
- Feature extraction over a rolling window of swaps (default: last 30 swaps or
  10 minutes, whichever fills first):
  - **price_impact** — max of (a) largest single-swap |Δtick| relative to the
    window's median |Δtick|, and (b) net one-way tick drift across the window
    (a uniform pump defeats (a) alone, so both are needed);
  - **volume_spike** — window |amount0| volume vs the rolling median window
    volume;
  - **concentration** — top sender's share of window volume (one wallet
    dominating = wash/manipulation signature);
  - **roundtrip** — share of volume from senders who both bought and sold
    inside the window (wash trading round-trips; real flow mostly doesn't).
- Scoring (transparent heuristic, weights documented in code and here):
  risk = 100 × (0.30·price_impact + 0.25·volume_spike + 0.25·concentration
  + 0.20·roundtrip), each feature normalized to 0–1.
  Action: risk ≥ 75 → Exit; 50–74 → Pause; < 50 → Hold.
  Confidence: grows with window sample size and with distance from the nearest
  action boundary (a risk of 51 is a coin-flip Pause; a risk of 95 is a sure
  Exit). The vault's `minConfidence` guardrail consumes exactly this number.
- Output: `{action, confidence, featuresHash}` where featuresHash is the
  SHA-256 of the canonical JSON of the feature vector — the full feature
  breakdown stays in the service log, its fingerprint goes on-chain.
- The service signs the decision digest with the model key (Phase 2: the
  agent wallet's second key / dedicated model key) and calls
  `submitDecision`. Signing/submission is Phase 2 wiring; Phase 1 ships the
  feature/scoring core plus the digest construction, proven by the self-test.

## Data flow (Phase 2 live loop)

1. Swap lands on the WETH/USDC pool (Base Sepolia) → Swap event.
2. Reactive Network delivers the log to SentinelReactive on Lasna.
3. RSC callback → AIOracle.recordObservation on Base Sepolia (event emitted).
4. Model service sees ObservationRecorded, scores the window, signs, submits
   decision to AIOracle.
5. Anyone (demo script) calls GuardianVault.actOnDecision → guardrails checked
   → vault pauses or partially exits to the safe address.
6. Evidence = the chain of tx hashes: swap → Lasna react → callback →
   observation → decision → vault action.

## Phase 1 status

- Contracts: SentinelReactive.sol, AIOracle.sol, GuardianVault.sol in `src/`,
  `forge build` (solc 0.8.26) must pass.
- Model service: `model_service.py` with `--selftest` synthetic cases
  (normal flow → Hold; wash burst → Pause/Exit; pump → Pause/Exit;
  concentrated whale → Pause/Exit). All cases must PASS.
- No deploys, no sends, wallet key untouched in Phase 1.
