"""
Integration test: Fully Funded Scenario (TestNet) with investor-initiated claims.
After funding:
- each investor calls "claim" to receive ASA
- creator calls "withdraw" to receive unlocked ALGO minus 2% to admin
- app account remains open (no closing)

Also asserts the 2% deposit is present before contributions (new deposit guard).
"""

from typing import Tuple, List

from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn
from algosdk.error import AlgodHTTPError

from deploy import get_clients, deploy_crowdfund, MIN_FEE, ProjectConfig

# ---- Provided mnemonics ----
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

def get_min_balance(client: algod.AlgodClient, addr: str) -> int:
    info = client.account_info(addr)
    return info.get("min-balance", 100_000)

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
    info = client.account_info(addr)
    total_local = info.get("total-app-local-states", 0)
    total_assets = info.get("total-assets-opted-in", len(info.get("assets", [])))
    if will_add_app_local:
        total_local += 1
    if will_add_asset_hold:
        total_assets += 1
    return BASE_MIN_BAL + PER_APP_LOCAL * total_local + PER_ASSET_HOLD * total_assets

def ensure_funds(client: algod.AlgodClient, funder_addr: str, funder_sk: str, target_addr: str, needed_contribution_algos: int, already_opted_app: bool, already_opted_asset: bool):
    min_bal = estimate_min_balance_after_optins(
        client, target_addr,
        will_add_app_local=not already_opted_app,
        will_add_asset_hold=not already_opted_asset
    )
    current = get_algo(client, target_addr)
    contribution_micro = microalgos(needed_contribution_algos)
    FEE_BUFFER = 4000
    required_total = min_bal + contribution_micro + FEE_BUFFER
    topup = max(0, required_total - current)
    if topup > 0:
        sp = client.suggested_params()
        pay = txn.PaymentTxn(sender=funder_addr, sp=sp, receiver=target_addr, amt=topup)
        stx = pay.sign(funder_sk)
        txid = client.send_transaction(stx)
        wait_for_confirmation(client, txid, 90)

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

    # NEW: assert deposit is present (at least deposit amount should be sitting on the app pre-contributions)
    app_start_algo = get_algo(algod_client, app_addr)
    assert app_start_algo >= deposit_amt, f"Deposit not present in app account (have {app_start_algo}, need >= {deposit_amt})"

    print("\n--- Balances BEFORE contributions ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # Investors OPT-IN to APP (local) — send individually (NOT as a group)
    opt1 = txn.ApplicationOptInTxn(sender=inv1_addr, sp=algod_client.suggested_params(), index=app_id)
    opt2 = txn.ApplicationOptInTxn(sender=inv2_addr, sp=algod_client.suggested_params(), index=app_id)
    send_and_wait(algod_client, [opt1.sign(inv1_sk)])
    send_and_wait(algod_client, [opt2.sign(inv2_sk)])
    print("Investors opted in (app).")

    # Investors OPT-IN to ASA (asset)
    opt_in_asset(algod_client, inv1_addr, inv1_sk, asa_id)
    opt_in_asset(algod_client, inv2_addr, inv2_sk, asa_id)
    print("Investors opted in (ASA).")

    # Ensure investors have enough spendable for contributions
    inv1_contrib_algos = 6
    inv2_contrib_algos = 4
    ensure_funds(algod_client, creator_addr, creator_sk, inv1_addr, inv1_contrib_algos, True, True)
    ensure_funds(algod_client, creator_addr, creator_sk, inv2_addr, inv2_contrib_algos, True, True)
    print("Investors funded sufficiently for contributions.")

    # Contributions (per investor: [AppCall("contribute"), Payment])
    def contribute(addr, sk, amount_algos: int):
        p_app = algod_client.suggested_params(); p_app.flat_fee = True; p_app.fee = MIN_FEE
        call = txn.ApplicationNoOpTxn(sender=addr, sp=p_app, index=app_id, app_args=[b"contribute"])
        pay  = txn.PaymentTxn(sender=addr, sp=algod_client.suggested_params(), receiver=app_addr, amt=microalgos(amount_algos))
        gid = txn.calculate_group_id([call, pay]); call.group = gid; pay.group = gid
        send_and_wait(algod_client, [call.sign(sk), pay.sign(sk)])

    contribute(inv1_addr, inv1_sk, inv1_contrib_algos)
    contribute(inv2_addr, inv2_sk, inv2_contrib_algos)
    print("Contributions complete.")

    print("\n--- Balances AFTER contributions ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # Investors claim ASA
    def claim(addr, sk):
        p = algod_client.suggested_params(); p.flat_fee = True; p.fee = MIN_FEE * 3
        call = txn.ApplicationNoOpTxn(
            sender=addr, sp=p, index=app_id,
            app_args=[b"claim"],
            foreign_assets=[asa_id],
        ).sign(sk)
        send_and_wait(algod_client, [call])

    claim(inv1_addr, inv1_sk)
    claim(inv2_addr, inv2_sk)
    print("Investors claimed ASA.")

    # Snapshot pre-withdraw balances for creator/admin/app
    creator_pre = get_algo(algod_client, creator_addr)
    admin_pre   = get_algo(algod_client, admin_addr)
    app_pre     = get_algo(algod_client, app_addr)

    # ---- Creator withdraw ----
    p = algod_client.suggested_params()
    p.flat_fee = True
    p.fee = MIN_FEE * 6  # inner payments: admin + creator (+ fees safety)
    withdraw = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=p,
        index=app_id,
        app_args=[b"withdraw"],
        accounts=[admin_addr],   # admin must be available to inner Payment
        foreign_assets=[asa_id], # not strictly needed here unless you add ASA consolidation
    ).sign(creator_sk)

    send_and_wait(algod_client, [withdraw])
    print("Creator withdraw complete.")

    print("\n--- Balances AFTER WITHDRAW ---")
    for a in [creator_addr, admin_addr, inv1_addr, inv2_addr, app_addr]:
        print_balances(algod_client, a, asa_id)

    # Assertions
    creator_post = get_algo(algod_client, creator_addr)
    admin_post   = get_algo(algod_client, admin_addr)
    app_post     = get_algo(algod_client, app_addr)

    admin_gain   = admin_post - admin_pre
    creator_gain = creator_post - creator_pre

    # Compute unlocked amount actually distributed (independent of min-balance/asset-close details)
    total_distributed = app_pre - app_post
    expected_admin_fee = (total_distributed * 2) // 100
    expected_creator_gain = total_distributed - expected_admin_fee

    print("\n--- Payout Summary (expected vs observed) ---")
    print(f"App pre-withdraw balance: {app_pre} µAlgos")
    print(f"App post-withdraw balance (min-balance remains): {app_post} µAlgos")
    print(f"Unlocked distributed: {total_distributed} µAlgos")
    print(f"Expected admin fee (2% of unlocked): {expected_admin_fee} µAlgos | Observed: {admin_gain} µAlgos")
    print(f"Expected creator remainder: {expected_creator_gain} µAlgos | Observed: {creator_gain} µAlgos")

    SLACK = 8_000  # allow for fee dust
    assert abs(admin_gain - expected_admin_fee) <= SLACK, "Admin fee mismatch"
    assert abs(creator_gain - expected_creator_gain) <= 20_000, "Creator payout mismatch"
    # App should retain at least its min-balance
    assert app_post >= get_min_balance(algod_client, app_addr), "App balance below min-balance"

if __name__ == "__main__":
    main()
