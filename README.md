
# Algorand Crowdfunding Escrow (PyTEAL)

This project implements a **stateful smart contract** that acts as an escrow for an Algorand-based crowdfunding campaign.

## Features

- Tracks per-investor contributions in **local state** (investor must opt-in).
- Accepts contributions (ALGO) and holds the project ASA in the **Application Address**.
- On success (goal reached before deadline):
  - Pays **2% admin fee** to the admin wallet.
  - Pays the remainder of ALGO to the developer.
  - **Returns the developer's 2% deposit**.
  - Investors can **claim ASA** at a fixed rate (`tokens_per_algo`).
- On failure (after deadline without reaching goal):
  - Investors can **refund** their ALGO.
  - The developer can **reclaim the ASA**.
  - The **developer/admin split the original deposit 50/50**.
- Uses **inner transactions**; fees are pooled from the app call txns.

> Note: The app requires enough minimum balance (for holding ALGO and ASA). Fund the app address accordingly at bootstrap.

## Contract Summary

Global state:
- `admin` (bytes) — admin address
- `dev` (bytes) — developer address
- `goal` (uint) — funding goal in microAlgos
- `deadline` (uint) — unix timestamp (seconds)
- `asa_id` (uint) — project ASA ID
- `rate` (uint) — tokens per 1 ALGOs (i.e., per 1_000_000 microAlgos)
- `raised` (uint) — sum of contributions (microAlgos)
- `finalized` (uint) — 0 (open), 1 (success), 2 (failed)
- `deposit` (uint) — developer 2% deposit (microAlgos)
- `required_pool` (uint) — tokens required if fully funded: `goal * rate / 1_000_000`

Local state (per investor):
- `contributed` (uint)
- `claimed` (uint, 0/1)

## Methods

- `create(admin, dev, goal, duration_secs, asa_id, rate)` — **ApplicationCreate** (grouped with a developer deposit Payment to the app address). `duration_secs` should be `60 * 24 * 60 * 60` for 60 days.
- `bootstrap()` — App-call first in a group to **opt-in** the ASA by inner tx, then expect a **developer ASA transfer** to the app address in the next group transaction for **at least** `required_pool`.
- `contribute()` — Group of 2: [0] payment from investor to app, [1] app call. Requires investor already **OptIn**'d to the app.
- `finalize_success()` — When `raised >= goal` and before deadline; pays admin fee (2%), pays remainder + returns the full developer **deposit**.
- `claim()` — After success finalize; investor transfers ASA owed = `contributed * rate / 1_000_000` and marks as claimed.
- `refund()` — After deadline and not funded; returns investor's contribution and marks as claimed.
- `close_fail()` — After deadline and not funded; splits developer deposit 50/50 to developer and admin, and sets `finalized=2`.
- `reclaim_asa()` — After failure `finalized=2`; sends all ASA back to developer.

All methods enforce **fee pooling**: callers must set a sufficient fee on the app call (multiples of `Global.min_txn_fee()`), as asserted by the contract.

## Test (Fully Funded Case)

The test script in `tests/test_full_funding.py`:
1. Creates admin, developer, and two investor accounts.
2. Creates a project ASA.
3. Deploys the app with a 60-day deadline.
4. Bootsraps (ASA opt-in + transfer required pool).
5. Investors opt-in and contribute (6 ALGO + 4 ALGO).
6. Finalizes success; verifies:
   - Admin got 2% of total raised.
   - Developer got the remainder + their returned deposit.
7. Investors `claim()` their ASA at the configured exchange rate.

> Configure endpoints with environment variables or edit the constants at the top of the script. The test assumes a local node (e.g., Sandbox or AlgoKit LocalNet) and **funded** accounts.

## Quick Start

1. **Install deps**
```bash
pip install pyteal==0.24.1 py-algorand-sdk==2.7.0
```
2. **Compile contract**
```bash
python scripts/compile.py
```
3. **Run the test (local node)**
```bash
python tests/test_full_funding.py
```

## Groups and Fees

- **Create**: Group size 2. [0] Payment (developer → app addr) **≥ deposit**, [1] AppCreate.
- **Bootstrap**: Group size 2. [0] AppCall `bootstrap`, [1] AssetTransfer (developer → app addr) **≥ required_pool`**.
- **Contribute**: Group size 2. [0] Payment (investor → app addr), [1] AppCall `contribute` from investor; ensure investor opted-in.
- **Finalize**: Single AppCall; must pay fee for 2 inner payments.
- **Claim/Refund**: Single AppCall; must pay fee for 1 inner transaction.

Make sure the app call txn fee covers the number of inner transactions the method will execute, i.e.:
- `finalize_success`: `Txn.fee >= 2 * Global.min_txn_fee()`
- `claim`/`refund`: `Txn.fee >= Global.min_txn_fee()`
- `close_fail`: `Txn.fee >= 2 * Global.min_txn_fee()`

## Security Notes

- Only **developer** or **admin** can call certain maintenance methods (`close_fail`, `reclaim_asa`) where appropriate.
- The contract performs strict **group structure checks**.
- The app treats `rate` as tokens per **1 ALGO**, not per microAlgo, to avoid precision blowups.

You may tune these to your exact product and UI flows.
