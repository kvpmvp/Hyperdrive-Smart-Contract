"""
Integration test: Fully Funded Scenario (TestNet) with oversubscription cap
Updated to match the new contract behavior:
- finalize distributes ASA to investors (batching via Txn.accounts)
- finalize then pays the AVAILABLE ALGO balance (Balance - MinBalance) to the developer
- no admin fee, no account/ASA close-out on success

Flow:
- Use provided mnemonics (creator, admin, investor1, investor2)
- Deploy ASA + App (goal, rate, 60 days)
- Investors opt-in to the app (local) and to the ASA (so they can receive tokens)
- Auto-top-up investor balances if needed (to cover min-balance + contribution + fees)
- Investors contribute so that total == goal
- Finalize: distributes ASA and pays available ALGO to creator (no admin fee / no closes)
- Assertions verify creator payout ≈ available balance and app retains its min-balance
"""

from typing import Tuple, List

from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn
from algosdk.error import AlgodHTTPError

from deploy import get_clients, deploy_crowdfund, MIN_FEE, ProjectConfig

# ---- REQUIRED MNEMONICS (from user) ----
CREATOR_MN = "able install flower toward cheap matter shallow switch dash roof suit eyebrow cheese current bleak enhance awesome brother leader they again simple desert about popular"
ADMIN_MN   = "science young voyage utility argue issue chase between dumb urban stone come hotel seat scorpion simple oak hub review gesture gossip smart city absent huge"
INV1_MN    = "damage cute radio venue palace stick double luggage round baby action fetch orchard pencil above slot water cement slot piano title gravity clutch absent sea"
INV2_MN    = "snow chat helmet surface enlist smile boss gesture region purse myth copper end link you trial sleep round vast tower farm tunnel humble able sentence"

# -------- Helpers --------

def addr_from_mn(mn: str) -> Tuple[str, str]:
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    return addr, sk

def wait_for_confirmation(client: algod.AlgodClient, txid: str, timeout: int = 90):
    """
    Robust waiter that tolerates 404s while the tx is propagating and
    advances rounds instead of tight-looping.
    """
    import time
    start = time.time()
    last_round = client.status().get("last-round")
    while True:
        try:
            p = client.pending_transaction_info(txid)
            cr = p.get("confirmed-round", 0)
            if cr and cr > 0:
                return p
        except AlgodHTTPError:
            pass
        last_round += 1
        client.status_after_block(last_round)
        if time.time() - start > timeout:
            raise TimeoutError(f"Transaction {txid} not confirmed after {timeout}s")

def microalgos(algos: int) -> int:
    return algos * 1_000_000

def get_algo(client: algod.AlgodClient, addr: str) -> int:
    return client.account_info(addr).get("amount", 0)

def print_balances(client: algod.AlgodClient, addr: str, asa_id: int):
    info = client.account_info(addr)
    algo = info.get("amount", 0)
    assets = {a["asset-id"]: a for a in info.get("assets", [])}
    asa_bal = assets.get(asa_id, {}).get("amount", 0)
    print(f"Account {addr}: ALGO={algo} µAlgos, ASA[{asa_id}]={asa_bal}")

def has_asset(client: algod.AlgodClient, addr: str, asa_id: int) -> bool:
    info = client.account_info(addr)
    for a in info.get("assets", []):
        if a.get("asset-id") == asa_id:
            return True
    return False

def opt_in_asset(client: algod.AlgodClient, addr: str, sk: str, asa_id: int):
    """ASA opt-in = zero-amount transfer to self."""
    if has_asset(client, addr, asa_id):
        return
    sp = client.suggested_params()
    t = txn.AssetTransferTxn(sender=addr, sp=sp, index=asa_id, receiver=addr, amt=0)
    stx = t.sign(sk)
    txid = client.send_transaction(stx)
    wait_for_confirmation(client, txid, timeout=90)

def send_and_wait(client: algod.AlgodClient, signed: List[txn.SignedTransaction]):
    """
    Send a group (or single) and wait for ALL txids. For ungrouped lists,
    submit individually instead of send_transactions.
    """
    if len(signed) == 1:
        txid = client.send_transaction(signed[0])
        wait_for_confirmation(client, txid, timeout=90)
        return [txid]
    else:
        # If they have a group field set, send as a group; else send individually.
        groups = {s.transaction.group for s in signed}
        if len(groups) == 1 and list(groups)[0] is not None and list(groups)[0] != b"":
            txids = [s.get_txid() for s in signed]
            client.send_transactions(signed)
            for tid in txids:
                wait_for_confirmation(client, tid, timeout=90)
            return txids
        else:
            txids = []
            for s in signed:
                txid = client.send_transaction(s)
                wait_for_confirmation(client, txid, timeout=90)
                txids.append(txid)
            return txids

# ---- Balance/top-up helpers ----

BASE_MIN_BAL = 100_000  # µAlgos
PER_APP_LOCAL = 100_000
PER_ASSET_HOLD = 100_000

def estimate_min_balance_after_optins(client: algod.AlgodClient, addr: str, will_add_app_local: bool, will_add_asset_hold: bool) -> int:
    """
    Estimate the Algorand min-balance for an account after new app local state
    and/or asset holding are added. Uses current totals + optional increments.
    """
    info = client.account_info(addr)
    total_local = info.get("total-app-local-states", 0)
    total_assets = info.get("total-assets-opted-in", len(info.get("assets", [])))
    # Some nodes expose explicit totals, others don't; fall back to lengths above.
    if will_add_app_local:
        total_local += 1
    if will_add_asset_hold:
        total_assets += 1
    return BASE_MIN_BAL + PER_APP_LOCAL * total_local + PER_ASSET_HOLD * total_assets

def ensure_funds(client: algod.AlgodClient, funder_addr: str, funder_sk: str, target_addr: str, needed_contribution_algos: int, already_opted_app: bool, already_opted_asset: bool):
    """
    Ensure target_addr has enough spendable balance to:
      min-balance (after required opt-ins) + contribution amount + fee buffer.
    If not, send a top-up PaymentTxn from funder.
    """
    min_bal = estimate_min_balance_after_optins(
        client, target_addr,
        will_add_app_local=not already_opted_app,
        will_add_asset_hold=not already_opted_asset
    )
    current = get_algo(client, target_addr)

    contribution_micro = microalgos(needed_contribution_algos)
    FEE_BUFFER = 4000  # µAlgos: a few transactions worth of fees
    required_total = min_bal + contribution_micro + FEE_BUFFER

    topup = max(0, required_total - current)
    if topup > 0:
        sp = client.suggested_params()
        pay = txn.PaymentTxn(sender=funder_addr, sp=sp, receiver=target_addr, amt=topup)
        stx = pay.sign(funder_sk)
        txid = client.send_transaction(stx)
        wait_for_confirmation(client, txid, 90)
        # Optional: sanity re-check
        assert get_algo(client, target_addr) >= required_total, "Top-up failed to reach required balance"

# -------- Test --------

def main():
    algod_client, _ = get_clients()

    creator_addr, creator_sk = addr_from_mn(CREATOR_MN)
    admin_addr, admin_sk     = addr_from_mn(ADMIN_MN)
    inv1_addr, inv1_sk       = addr_from_mn(INV1_MN)
    inv2_addr, inv2_sk       = addr_from_mn(INV2_MN)

    # Deploy (goal=10 ALGO, rate=100 tokens/ALGO, 60 days)
    cfg = ProjectConfig(goal_algos=10, rate_per_algo=100, days_duration=60)
    app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt = deploy_crowdfund(CREATOR_MN, ADMIN_MN, cfg)
    print(f"App {app_id} at {app_addr}, ASA {asa_id}, expected tokens {tokens_expected}, deposit {deposit_amt} µAlgos")

    print("\n--- Balances BEFORE contributions ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # --- Investors OPT-IN to APP (local state) ---
    opt_params = algod_client.suggested_params()
    stx1 = txn.ApplicationOptInTxn(sender=inv1_addr, sp=opt_params, index=app_id).sign(inv1_sk)
    stx2 = txn.ApplicationOptInTxn(sender=inv2_addr, sp=algod_client.suggested_params(), index=app_id).sign(inv2_sk)
    send_and_wait(algod_client, [stx1])  # send separately to avoid accidental group
    send_and_wait(algod_client, [stx2])
    print("Investors opted in (app).")

    # --- Investors OPT-IN to ASA (asset holding) ---
    opt_in_asset(algod_client, inv1_addr, inv1_sk, asa_id)
    opt_in_asset(algod_client, inv2_addr, inv2_sk, asa_id)
    print("Investors opted in (ASA).")

    # --- Ensure investors have enough spendable for contributions ---
    inv1_contrib_algos = 6
    inv2_contrib_algos = 4
    ensure_funds(
        algod_client, creator_addr, creator_sk,
        inv1_addr, inv1_contrib_algos,
        already_opted_app=True, already_opted_asset=True
    )
    ensure_funds(
        algod_client, creator_addr, creator_sk,
        inv2_addr, inv2_contrib_algos,
        already_opted_app=True, already_opted_asset=True
    )
    print("Investors funded sufficiently for contributions.")

    # --- Contributions (grouped per investor: AppCall + Payment) ---
    def contribute(addr, sk, amount_algos: int):
        params_app = algod_client.suggested_params()
        params_app.flat_fee = True
        params_app.fee = MIN_FEE  # appcall; no inner txns here

        app_call = txn.ApplicationNoOpTxn(
            sender=addr, sp=params_app, index=app_id, app_args=[b"contribute"]
        )
        pay = txn.PaymentTxn(
            sender=addr, sp=algod_client.suggested_params(), receiver=app_addr, amt=microalgos(amount_algos)
        )

        gid = txn.calculate_group_id([app_call, pay])
        app_call.group = gid
        pay.group = gid

        stx_a = app_call.sign(sk)
        stx_b = pay.sign(sk)
        send_and_wait(algod_client, [stx_a, stx_b])

    contribute(inv1_addr, inv1_sk, inv1_contrib_algos)
    contribute(inv2_addr, inv2_sk, inv2_contrib_algos)
    print("Contributions complete.")

    print("\n--- Balances AFTER contributions ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # Snapshot pre-finalize balances for assertions
    creator_pre = get_algo(algod_client, creator_addr)
    admin_pre   = get_algo(algod_client, admin_addr)
    app_pre     = get_algo(algod_client, app_addr)  # should be ~goal + deposit

    # --- Finalize success: distributes ASA, sends AVAILABLE ALGO to creator (no admin fee, no closes) ---
    params = algod_client.suggested_params()
    params.flat_fee = True
    # Inner txs: up to 2 ASA sends + 1 payment to creator; budget generously
    params.fee = MIN_FEE * 8

    finalize = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=params,
        index=app_id,
        app_args=[b"finalize"],
        accounts=[inv1_addr, inv2_addr],  # NOTE: contract pays sender + these accounts
        foreign_assets=[asa_id],
    ).sign(creator_sk)

    send_and_wait(algod_client, [finalize])
    print("Finalize complete.")

    print("\n--- Balances AFTER FINALIZE ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # Assertions: no admin fee, creator gets app's AVAILABLE balance, app retains min-balance
    creator_post = get_algo(algod_client, creator_addr)
    admin_post   = get_algo(algod_client, admin_addr)
    app_post     = get_algo(algod_client, app_addr)

    app_info_post = algod_client.account_info(app_addr)
    app_min_bal_post = app_info_post.get("min-balance", 0)

    admin_gain   = admin_post - admin_pre
    creator_gain = creator_post - creator_pre

    expected_creator_gain = app_pre - app_min_bal_post  # available = balance - min balance

    print("\n--- Payout Summary (expected vs observed) ---")
    print(f"App pre-finalize balance: {app_pre} µAlgos")
    print(f"App min-balance after finalize: {app_min_bal_post} µAlgos")
    print(f"Expected creator payout (available): {expected_creator_gain} µAlgos | Observed: {creator_gain} µAlgos")
    print(f"Admin gain (should be 0): {admin_gain} µAlgos")
    print(f"App post-finalize balance (should equal min-balance): {app_post} µAlgos")

    SLACK = 20_000  # allow for outer/inner fee dust
    assert admin_gain == 0, "Admin should not receive funds in new finalize flow"
    assert abs(creator_gain - expected_creator_gain) <= SLACK, "Creator payout mismatch"
    assert app_post == app_min_bal_post, "App balance should remain at min-balance (no close-out)"

if __name__ == "__main__":
    main()
