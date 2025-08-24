"""
Unfunded path integration test (TestNet, ~2 minutes):
- Deploy app with a short deadline (≈40 rounds)
- Make partial contributions (sum < goal)
- Wait past deadline, then investors call "refund"
- Optionally, creator calls "reclaim" (deposit split; may be limited by min-balance)
- Finally, creator closes the vault (sweep ASA holding and close ALGO)

Also asserts the deposit is present before contributions (new deposit guard).
"""

from typing import Tuple, List

from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn
from algosdk.error import AlgodHTTPError
from algosdk.logic import get_application_address
from algosdk.encoding import decode_address

from pyteal import compileTeal, Mode
from crowdfunding_contract import approval_program, clear_program

# ---- Provided mnemonics ----
CREATOR_MN = "able install flower toward cheap matter shallow switch dash roof suit eyebrow cheese current bleak enhance awesome brother leader they again simple desert about popular"
ADMIN_MN   = "science young voyage utility argue issue chase between dumb urban stone come hotel seat scorpion simple oak hub review gesture gossip smart city absent huge"
INV1_MN    = "damage cute radio venue palace stick double luggage round baby action fetch orchard pencil above slot water cement slot piano title gravity clutch absent sea"
INV2_MN    = "snow chat helmet surface enlist smile boss gesture region purse myth copper end link you trial sleep round vast tower farm tunnel humble able sentence"

MIN_FEE = 1000  # µAlgos
BASE_MIN_BAL   = 100_000
PER_APP_LOCAL  = 100_000

def addr_from_mn(mn: str) -> Tuple[str, str]:
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    return addr, sk

def microalgos(algos: int) -> int:
    return algos * 1_000_000

def get_algo(client: algod.AlgodClient, addr: str) -> int:
    return client.account_info(addr).get("amount", 0)

def print_balances(client: algod.AlgodClient, addr: str, label: str = ""):
    info = client.account_info(addr)
    algo = info.get("amount", 0)
    if label:
        print(f"{label} {addr}: ALGO={algo} µAlgos")
    else:
        print(f"{addr}: ALGO={algo} µAlgos")

def robust_wait_for_confirmation(client: algod.AlgodClient, txid: str, timeout: int = 120):
    import time
    start = time.time()
    last_round = client.status().get("last-round", 0)
    while True:
        try:
            p = client.pending_transaction_info(txid)
            if p.get("pool-error"):
                raise Exception(f"Pool error for {txid}: {p['pool-error']}")
            cr = p.get("confirmed-round", 0)
            if cr and cr > 0:
                return p
        except AlgodHTTPError:
            pass
        last_round = max(last_round, client.status().get("last-round", 0)) + 1
        client.status_after_block(last_round)
        if time.time() - start > timeout:
            raise TimeoutError(f"Transaction {txid} not confirmed after {timeout}s")

def send_and_wait(client: algod.AlgodClient, signed: List[txn.SignedTransaction]):
    if len(signed) == 1:
        txid = client.send_transaction(signed[0])
        robust_wait_for_confirmation(client, txid, timeout=120)
        return [txid]
    groups = {s.transaction.group for s in signed}
    if len(groups) == 1 and list(groups)[0]:
        txids = [s.get_txid() for s in signed]
        client.send_transactions(signed)
        for t in txids:
            robust_wait_for_confirmation(client, t, timeout=120)
        return txids
    txids = []
    for s in signed:
        txid = client.send_transaction(s)
        robust_wait_for_confirmation(client, txid, timeout=120)
        txids.append(txid)
    return txids

def wait_for_round(client: algod.AlgodClient, target_round: int):
    lr = client.status()["last-round"]
    while lr < target_round:
        client.status_after_block(lr + 1)
        lr += 1

def estimate_min_balance_after_optins(client: algod.AlgodClient, addr: str, will_add_app_local: bool) -> int:
    info = client.account_info(addr)
    total_local = info.get("total-app-local-states", 0)
    if will_add_app_local:
        total_local += 1
    return BASE_MIN_BAL + PER_APP_LOCAL * total_local

def ensure_funds_for_contribution(client: algod.AlgodClient, funder_addr: str, funder_sk: str,
                                  target_addr: str, needed_contribution_algos: int,
                                  will_add_app_local: bool):
    min_bal = estimate_min_balance_after_optins(client, target_addr, will_add_app_local)
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
        robust_wait_for_confirmation(client, txid, 120)

def get_clients():
    import os
    algod_token = os.environ.get("ALGOD_TOKEN", "")
    algod_address = os.environ.get("ALGOD_URL", "https://testnet-api.algonode.cloud")
    headers = {"X-API-Key": algod_token} if algod_token else {}
    algod_client = algod.AlgodClient(algod_token, algod_address, headers)
    return algod_client, None

def compile_program_bytes(algod_client: algod.AlgodClient, teal_source: str) -> bytes:
    import base64
    resp = algod_client.compile(teal_source)
    return base64.b64decode(resp["result"])

def current_round(algod_client: algod.AlgodClient) -> int:
    return algod_client.status()["last-round"]

def deploy_quick_deadline(algod_client: algod.AlgodClient,
                          creator_mn: str, admin_mn: str,
                          goal_algos: int, rate_per_algo: int,
                          deadline_rounds: int):
    creator_sk = mnemonic.to_private_key(creator_mn)
    creator_addr = account.address_from_private_key(creator_sk)
    admin_sk = mnemonic.to_private_key(admin_mn)
    admin_addr = account.address_from_private_key(admin_sk)

    goal_micro = goal_algos * 1_000_000
    tokens_expected = goal_algos * rate_per_algo
    deposit_amt = (goal_micro * 2) // 100  # 2%

    deadline = current_round(algod_client) + int(deadline_rounds)

    approval_teal = compileTeal(approval_program(), mode=Mode.Application, version=8)
    clear_teal    = compileTeal(clear_program(),     mode=Mode.Application, version=8)
    approval_prog = compile_program_bytes(algod_client, approval_teal)
    clear_prog    = compile_program_bytes(algod_client, clear_teal)

    # --- CHANGE 1: Let node compute the minimum fee for AppCreate ---
    sp_app = algod_client.suggested_params()
    sp_app.flat_fee = False  # let node estimate min fee to avoid underpay

    # --- CHANGE 2: Update global schema to 9 uints (fee_paid + contrib_count added) ---
    create_txn = txn.ApplicationCreateTxn(
        sender=creator_addr, sp=sp_app,
        on_complete=txn.OnComplete.NoOpOC.real,
        approval_program=approval_prog,
        clear_program=clear_prog,
        global_schema=txn.StateSchema(num_uints=9, num_byte_slices=2),  # <-- was 8
        local_schema=txn.StateSchema(num_uints=1, num_byte_slices=0),
        app_args=[
            decode_address(admin_addr),
            goal_micro.to_bytes(8, "big"),
            rate_per_algo.to_bytes(8, "big"),
            int(deadline).to_bytes(8, "big"),
        ],
    )

    stx = create_txn.sign(creator_sk)
    try:
        txid = algod_client.send_transaction(stx)
    except AlgodHTTPError as e:
        # Surface helpful error details when node rejects the tx
        print("AlgodHTTPError during AppCreate:", getattr(e, "args", e))
        raise
    ptx = robust_wait_for_confirmation(algod_client, txid, 120)
    app_id = ptx["application-index"]
    app_addr = get_application_address(app_id)

    # Create ASA
    sp = algod_client.suggested_params(); sp.flat_fee = True; sp.fee = MIN_FEE
    atxn = txn.AssetCreateTxn(
        sender=creator_addr, sp=sp,
        total=tokens_expected,
        default_frozen=False,
        unit_name="PRJ", asset_name="ProjectToken",
        manager=creator_addr, reserve=creator_addr,
        freeze=None, clawback=None, decimals=0
    )
    stx = atxn.sign(creator_sk)
    txid = algod_client.send_transaction(stx)
    ptx = robust_wait_for_confirmation(algod_client, txid, 120)
    asa_id = ptx.get("asset-index")

    # Setup group: deposit -> AppCall("setup", ["setup", asa_id]) -> seed ASA to app
    sp_pay = algod_client.suggested_params(); sp_pay.flat_fee = True; sp_pay.fee = MIN_FEE
    pay_deposit = txn.PaymentTxn(sender=creator_addr, sp=sp_pay, receiver=app_addr, amt=deposit_amt)

    sp_setup = algod_client.suggested_params(); sp_setup.flat_fee = True; sp_setup.fee = MIN_FEE * 2
    setup_call = txn.ApplicationNoOpTxn(
        sender=creator_addr, sp=sp_setup, index=app_id,
        app_args=[b"setup", int(asa_id).to_bytes(8, "big")],
        foreign_assets=[asa_id],
    )

    sp_xfer = algod_client.suggested_params(); sp_xfer.flat_fee = True; sp_xfer.fee = MIN_FEE
    seed_tokens = txn.AssetTransferTxn(sender=creator_addr, sp=sp_xfer, index=asa_id,
                                       receiver=app_addr, amt=tokens_expected)

    gid = txn.calculate_group_id([pay_deposit, setup_call, seed_tokens])
    pay_deposit.group = gid; setup_call.group = gid; seed_tokens.group = gid

    send_and_wait(algod_client, [pay_deposit.sign(creator_sk),
                                 setup_call.sign(creator_sk),
                                 seed_tokens.sign(creator_sk)])

    return app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt, deadline

def main():
    algod_client, _ = get_clients()

    creator_addr, creator_sk = addr_from_mn(CREATOR_MN)
    admin_addr,   admin_sk   = addr_from_mn(ADMIN_MN)
    inv1_addr,    inv1_sk    = addr_from_mn(INV1_MN)
    inv2_addr,    inv2_sk    = addr_from_mn(INV2_MN)

    # Deploy quick deadline: goal=10 ALGO, rate=100 tokens/ALGO, deadline ≈ 40 rounds
    app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt, deadline_round = deploy_quick_deadline(
        algod_client, CREATOR_MN, ADMIN_MN, goal_algos=10, rate_per_algo=100, deadline_rounds=40
    )

    # NEW: assert deposit is present before contributions
    assert get_algo(algod_client, app_addr) >= deposit_amt, "Deposit not present on app before contributions"

    print(f"App {app_id} at {app_addr}, ASA {asa_id}, deposit {deposit_amt} µAlgos, deadline @ round {deadline_round}")

    print("\n--- Balances BEFORE contributions ---")
    for label, addr in [("Creator", creator_addr), ("Admin", admin_addr),
                        ("Investor1", inv1_addr), ("Investor2", inv2_addr),
                        ("App", app_addr)]:
        print_balances(algod_client, addr, label)

    # Investors OPT-IN to APP (local), individually
    opt1 = txn.ApplicationOptInTxn(sender=inv1_addr, sp=algod_client.suggested_params(), index=app_id)
    opt2 = txn.ApplicationOptInTxn(sender=inv2_addr, sp=algod_client.suggested_params(), index=app_id)
    send_and_wait(algod_client, [opt1.sign(inv1_sk)])
    send_and_wait(algod_client, [opt2.sign(inv2_sk)])
    print("Investors opted in (app).")

    # Ensure investors are funded enough to make contributions (accounting for new local state)
    inv1_contrib_algos = 3
    inv2_contrib_algos = 2
    def ensure_funds_for_contribution_local(*args, **kwargs):
        return ensure_funds_for_contribution(*args, **kwargs)
    ensure_funds_for_contribution_local(algod_client, creator_addr, creator_sk, inv1_addr, inv1_contrib_algos, will_add_app_local=False)
    ensure_funds_for_contribution_local(algod_client, creator_addr, creator_sk, inv2_addr, inv2_contrib_algos, will_add_app_local=False)

    # Contributions (below goal)
    def contribute(addr, sk, amount_algos: int):
        p_app = algod_client.suggested_params(); p_app.flat_fee = True; p_app.fee = MIN_FEE
        call = txn.ApplicationNoOpTxn(sender=addr, sp=p_app, index=app_id, app_args=[b"contribute"])
        pay  = txn.PaymentTxn(sender=addr, sp=algod_client.suggested_params(),
                              receiver=app_addr, amt=microalgos(amount_algos))
        gid = txn.calculate_group_id([call, pay]); call.group = gid; pay.group = gid
        send_and_wait(algod_client, [call.sign(sk), pay.sign(sk)])

    contribute(inv1_addr, inv1_sk, inv1_contrib_algos)
    contribute(inv2_addr, inv2_sk, inv2_contrib_algos)
    print("Partial contributions complete (sum below goal).")

    print("\nWaiting to pass deadline ...")
    wait_for_round(algod_client, deadline_round + 1)

    # Refunds
    def refund(addr, sk):
        p = algod_client.suggested_params(); p.flat_fee = True; p.fee = MIN_FEE * 3
        call = txn.ApplicationNoOpTxn(sender=addr, sp=p, index=app_id, app_args=[b"refund"])
        send_and_wait(algod_client, [call.sign(sk)])

    refund(inv1_addr, inv1_sk)
    refund(inv2_addr, inv2_sk)
    print("Investors refunded.")

    # ---- Creator reclaims deposit (one-time), sweeps ASA holding, then closes the vault ----
    print("\nCreator reclaiming deposit...")
    p_reclaim = algod_client.suggested_params()
    p_reclaim.flat_fee = True
    p_reclaim.fee = MIN_FEE * 5  # two inner payments (admin + creator)
    reclaim_call = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=p_reclaim,
        index=app_id,
        app_args=[b"reclaim"],
        accounts=[admin_addr],  # optional; receivers are set in inner tx; included for clarity
    ).sign(creator_sk)
    send_and_wait(algod_client, [reclaim_call])
    print("reclaim complete.")

    print("Sweeping ASA back to creator (failed path)...")
    p_sweep = algod_client.suggested_params()
    p_sweep.flat_fee = True
    p_sweep.fee = MIN_FEE * 3  # inner asset close-out
    sweep_call = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=p_sweep,
        index=app_id,
        app_args=[b"sweep_asa_failed"],
        foreign_assets=[asa_id],  # not strictly required for inner xfer, but harmless
    ).sign(creator_sk)
    send_and_wait(algod_client, [sweep_call])
    print("sweep_asa_failed complete.")

    print("Closing vault (failure path)...")
    p_close = algod_client.suggested_params()
    p_close.flat_fee = True
    p_close.fee = MIN_FEE * 4  # inner payment with close_remainder_to (+ safety)
    close_vault = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=p_close,
        index=app_id,
        app_args=[b"close_vault"],
        accounts=[app_addr],       # needed for AssetHolding.balance(app_addr, asa_id)
        foreign_assets=[asa_id],   # make ASA available to TEAL for AssetHolding.*
    ).sign(creator_sk)
    send_and_wait(algod_client, [close_vault])
    print("close_vault executed. App account should now be emptied/closed.")

    # Optional: Check app account (may error if fully closed)
    try:
        info = algod_client.account_info(app_addr)
        print(f"Post-close_vault app balance: {info.get('amount', 0)} µAlgos, min-balance={info.get('min-balance', 0)}")
    except Exception as e:
        print(f"App account likely closed (lookup failed): {e}")

    print("\nUNFUNDED TEST COMPLETE ✅")

if __name__ == "__main__":
    main()
