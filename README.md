# AI Risk Sentinel

**Reactive Omni Buildathon — "Reactive Meets AI" track.**

A Reactive Smart Contract on **Lasna** watches every `Swap` on a real Uniswap V3
pool on **Base Sepolia** and forwards it on-chain to an AI oracle; an off-chain
model service scores the swap flow for manipulation-like patterns (wash trading,
pumps, whale concentration) and posts an ECDSA-signed decision —
**hold / pause / exit** — to the oracle; a guardian vault on Base Sepolia then
acts on the decision under hard contract-side guardrails the model cannot
override (confidence threshold, cooldown, per-action exit cap, staleness
rejection, owner override).

The AI is a running part of the system, not a coding aid: no decision exists
unless the model service computes features, scores them, signs the result, and
submits it on-chain.

Testnets only. Zero own money — all funds are faucet/bridge testnet assets in a
dedicated agent wallet.

## Architecture

```
 Base Sepolia (84532)                     Lasna (5318007)              Base Sepolia (84532)
 ┌───────────────────┐   Swap event      ┌──────────────────┐  callback  ┌──────────────────┐
 │ Uniswap V3 pool   │ ────────────────► │ SentinelReactive │ ─────────► │ AIOracle         │
 │ WETH/USDC 0.3%    │   (Reactive       │ (RSC)            │ recordObs. │ · observations   │
 └───────────────────┘    subscription)  └──────────────────┘            │ · signed decisions│
        ▲                                                                └───────┬──────────┘
        │ swaps (demo driver)                                                  │ ObservationRecorded
        │                                                                      ▼
 ┌──────┴──────────┐                                                  ┌──────────────────┐
 │ DemoTrader x N  │                                                  │ model_service.py │
 └─────────────────┘                                                  │ features + score │
                                                                      │ sign + submit    │
 ┌───────────────────┐   actOnDecision (guardrails enforced here)     └───────┬──────────┘
 │ GuardianVault     │ ◄─────────────────────────────────────────────────────┘
 │ · pause / exit    │            decision consumed exactly once via AIOracle.markExecuted
 └───────────────────┘
```

Data flow, step by step:

1. A swap lands on the watched pool → `Swap` event on Base Sepolia.
2. The Reactive Network delivers the log to `SentinelReactive` on Lasna
   (`SYSTEM.subscribe` in the constructor; `react()` is `onlySystem`).
3. The RSC decodes the swap (amounts, tick, sender) and requests a callback via
   `SYSTEM.requestCallbackV_1_0` → `AIOracle.recordObservation(...)` executes on
   Base Sepolia. The system contract stamps the RSC's address into the payload,
   and the oracle only accepts allowlisted RSCs.
4. The model service polls `ObservationRecorded` events, maintains a rolling
   window (last 30 swaps / 10 min), computes features, scores risk, signs the
   decision digest (`personal_sign`, verified on-chain by `ecrecover` against
   `modelSigner`), and calls `AIOracle.submitDecision`.
5. Anyone calls `GuardianVault.actOnDecision(decisionId)`. The vault checks the
   guardrails in-contract and pauses the vault or exits up to 50% of its
   balance to a fixed safe address. Each decision is consumed exactly once.

## The model

Transparent heuristic — weights and thresholds are in `model_service.py` and
documented there and in `BUILD.md`. Features (each normalized 0–1 over the
rolling window):

| Feature | Weight | What it catches |
|---|---|---|
| `price_impact` | 0.30 | Largest single-swap tick jump vs the window median, **or** net one-way tick drift (a uniform pump is invisible to the first, not the second) |
| `volume_spike` | 0.25 | Window volume vs the rolling baseline window volume (4× saturates) |
| `concentration` | 0.25 | Top sender's share of window volume — one wallet dominating is the wash/manipulation signature |
| `roundtrip` | 0.20 | Volume share from senders who both bought **and** sold inside the window — wash trading round-trips; organic flow mostly doesn't |

`risk = 100 × Σ weight·feature`; **risk ≥ 75 → exit, 50–74 → pause, < 50 → hold**.
Confidence grows with window sample size and with the distance of the risk
score from the nearest action boundary — the vault's `minConfidence` guardrail
consumes exactly this number, so a borderline call is recorded but not acted
on. `featuresHash` (SHA-256 of the canonical feature JSON) goes on-chain with
every decision; the full feature breakdown stays in the service log.

## Contract map

| File | Chain | Role |
|---|---|---|
| `src/SentinelReactive.sol` | Lasna 5318007 | RSC: subscribes to the pool's Swap topic, decodes logs in `react()`, requests callbacks to the oracle |
| `src/AIOracle.sol` | Base Sepolia 84532 | Meeting point: RSC-allowlisted `recordObservation`; `submitDecision` with on-chain ECDSA verification vs `modelSigner`; vault-only `markExecuted` (exactly-once consumption) |
| `src/GuardianVault.sol` | Base Sepolia 84532 | Demo vault holding testnet ETH. `actOnDecision` enforces: confidence ≥ 60, 300 s cooldown, Exit ≤ 50% of balance per action to a fixed `safeAddress`, decisions older than 1 h ignored. Pause blocks owner withdrawals; owner override (`setPaused`, `emergencyWithdraw`) always works |
| `src/DemoTrader.sol` | Base Sepolia 84532 | Demo helper: holds WETH/USDC and swaps directly against the pool so each instance is a distinct Swap `sender` (the SwapRouter would collapse all senders into one and destroy the concentration/roundtrip features) |
| `src/GateOrigin.sol`, `src/GateReactive.sol`, `src/GateDestination.sol` | Base Sepolia / Lasna | Phase-0 gate contracts: the minimal Ping → react → callback loop used to prove the Omni pattern end-to-end before building the sentinel |

Deployed addresses and every tx hash are in `LOG.md` (the build log is the
evidence index).

## Repository layout

| Path | What |
|---|---|
| `model_service.py` | The off-chain model: feature extraction, scoring, confidence, decision digest. Stdlib only. `python3 model_service.py --selftest` runs the offline synthetic cases |
| `phase2_driver.py` | Phase 2 live driver: benign/attack swap legs, observation polling + window rebuild, sign + submit + act |
| `phase3_driver.py` | Phase 3: Exit-path exercise (bridge, T0 top-up, calibrated exit sequences, cooldown-checked decide) |
| `BUILD.md` | Design notes: architecture, data flow, guardrails, evidence chain |
| `LOG.md` | Full build log with deployed addresses and the complete tx-hash evidence chains (gate, benign, attack, exit) |
| `phase2_state.json`, `phase2_window.json`, `phase2_evidence.json` | Machine-readable run state, the scored observation windows, and the raw tx evidence |
| `foundry.toml`, `src/`, `out/` | Foundry project (solc 0.8.26) |

## Running the model service

Offline verification (no chain, no keys):

```bash
python3 model_service.py --selftest
# normal flow -> hold | wash burst -> pause/exit | pump -> pause/exit |
# concentrated whale -> pause/exit     (4/4 must PASS)
```

Live loop (as run in Phase 2/3 — see `LOG.md` for the full record):

```bash
export PATH="$HOME/.foundry/bin:$PATH"
python3 phase2_driver.py benign        # fire the benign swap leg
python3 phase2_driver.py wait-obs 24 600   # wait for RSC callbacks, rebuild window
python3 phase2_driver.py score         # dry-run decision on the window
python3 phase2_driver.py decide benign # sign + submitDecision + actOnDecision
python3 phase2_driver.py attack1       # wash batch ... (attack2/attack3 calibrate)
python3 phase2_driver.py decide attack
python3 phase3_driver.py exit1         # calibrated exit-band sequence
python3 phase3_driver.py decide-exit   # cooldown-checked exit submission + action
```

Keys live in `keys/` (mode 600, never committed to any remote, never printed):
`keys/wallet.txt` is the agent wallet that deploys and drives the demo;
`keys/model.txt` is the dedicated model signer whose signatures the oracle
verifies. Compiling the contracts: `forge build`.

## Demo evidence (all on-chain, all verifiable)

- **Gate** — minimal Ping → react → callback loop across Base Sepolia ↔ Lasna.
- **Benign leg** — 24 ordinary swaps → decision **HOLD** (risk 6.46); the
  vault's confidence guardrail consumed the decision without acting, exactly
  as designed.
- **Unscripted catch** — a genuine third-party 1,000 USDC swap hit the pool
  mid-demo and was scored Pause-grade by the sentinel, unscripted.
- **Attack leg** — wash + one-way press pattern → decision **PAUSE** (risk
  65.26, confidence 60) → vault paused on-chain; owner withdrawal while paused
  reverted; owner override restored control.
- **Exit leg** — calibrated high-risk window → decision **EXIT** → vault moved
  50% of its balance to the safe address under the cap.

Full tx hashes for every step: `LOG.md`.

## Testnet-demo vs production hardening

Demo-grade by design: heuristic model, single model signer, demo vault holding
test ETH, self-driven swap flow on a sparse testnet pool. A production version
would need: a trained/validated model with labeled manipulation data, signer
key management (HSM/rotation, multi-sig oracle updates), monitoring of RSC
reserves and callback debt, per-pool baselines, and audits of the vault's
guardrail logic. The architecture — reactive event delivery, signed decisions,
in-contract guardrails, exactly-once consumption — is the part meant to carry
over unchanged.
