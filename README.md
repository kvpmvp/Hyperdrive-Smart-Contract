# Algorand Crowdfunding (PyTeal + Python SDK)

This package implements a **stateful smart contract** that holds ALGO contributions and a developer ASA, for a crowdfunding campaign with:

- Fixed **goal** (in ALGOs)
- Fixed **exchange rate** (X ASA / 1 ALGO)
- **2% deposit** from creator at setup
- Success path: distribute ASA to contributors, pay **2% admin fee**, close remainder to creator
- Failure path: refund contributors, split deposit (50% admin / 50% creator), close to creator
- App self-closes when done.

## Files

- `crowdfunding_contract.py` — PyTeal approval/clear programs
- `deploy.py` — compile, deploy, create ASA, and run setup (deposit + ASA seed)
- `test_funded.py` — integration test for the **fully funded** case using your provided mnemonics

## Quick Start

1. **Install deps (Python 3.10+)**

```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install pyteal==0.24.1 py-algorand-sdk==2.6.2
```

2. **Run the funded test** (uses TestNet public Algonode endpoints)

```bash
python test_funded.py
```

This will:

- Create an ASA (decimals 0) sized to `rate * goal`
- Deploy the app with:
  - `goal=10 ALGO`
  - `rate=100 ASA / ALGO`
  - `deadline≈60 days` (round-based)
- Perform `setup` (creator deposit=2% and seed ASA to app)
- Investor 1 contributes **6 ALGO**, Investor 2 contributes **4 ALGO**
- `finalize` distributes ASA to both investors, pays admin 2%, and **closes** account balances.

3. **What you can tweak**

- In `test_funded.py`, edit the `ProjectConfig` or contribution amounts.
- Use a real ASA by setting `asa_id` in `setup` path if you prefer. Current code **creates** a fresh ASA for the demo.

## Notes and Constraints

- **Accounts Limit:** Finalization/refund loops over `Txn.accounts`. Pass all investors you want to settle in each call. Multiple calls are allowed; once `raised==0`, the app self-closes.
- **Rounding:** `tokens = (contribution_microalgos * rate) / 1_000_000`. Fractional parts are truncated.
- **Fees:** The outer AppCall must include extra fee to cover **inner transactions** (we set flat fees in scripts).
- **Deadline:** Implemented as **round-based** (~3.7s/round estimate).

## Security & Production Considerations

- Add checks to prevent over-funding if not desired.
- Consider an explicit allowlist of investors if you need KYC/AML gates.
- In production, handle edge cases (dust balances, zero rates, non-opted investors).
- For many investors, prefer a **"claim"** method instead of passing all in `accounts`.

---

Public endpoints used:
- Algod: `https://testnet-api.algonode.cloud`
- Indexer: `https://testnet-idx.algonode.cloud`
