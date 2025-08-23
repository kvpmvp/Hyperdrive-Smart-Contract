"""
Negative test: contributions are rejected unless the developer has paid the 2% deposit
and completed setup. We create the app (without running setup/deposit) and then try to
contribute. Expected: logic eval error (assert failed) due to deposit guard.
"""

from typing import Tuple, List

from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn
from algosdk.error import AlgodHTTPError
from algosdk.encoding import decode_address
from algosdk.logic import get_application_address

from pyteal import compileTeal, Mode
from crowdfunding_contract import approval_program, clear_program

# ---- Provided mnemonics ----
CREATOR_MN = "able install flower toward cheap matter shallow switch dash roof suit eyebrow cheese current bleak enhance awesome brother leader they again simple desert about popular"
ADMIN_MN   = "science young voyage utility argue issue chase between dumb urban stone come hotel seat scorpion simple oak hub review gesture gossip smart city absent huge"
INV1_MN    = "damage cute radio venue palace stick double luggage round baby action fetch orchard pencil above slot water cement slot piano title gravity clutch absent sea"

MIN_FEE = 1000  # µAlgos

def addr_from_mn(mn: str) -> Tuple[str, str]:
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    return addr, sk

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

def main():
    # Client
    import os
    algod_token = os.environ.get("ALGOD_TOKEN", "")
    algod_address = os.environ.get("ALGOD_URL", "https://testnet-api.algonode.cloud")
    headers = {"X-API-Key": algod_token} if algod_token else {}
    client = algod.AlgodClient(algod_token, algod_address, headers)

    creator_addr, creator_sk = addr_from_mn(CREATOR_MN)
    admin_addr,   _          = addr_from_mn(ADMIN_MN)
    inv1_addr,    inv1_sk    = addr_from_mn(INV1_MN)

    # Small project params
    goal_algos = 5
    rate_per_algo = 100
    goal_micro = goal_algos * 1_000_000

    # Compile app
    approval_teal = compileTeal(approval_program(), mode=Mode.Application, version=8)
    clear_teal    = compileTeal(clear_program(),     mode=Mode.Application, version=8)
    import base64
    ap = base64.b64decode(client.compile(approval_teal)["result"])
    cp = base64.b64decode(client.compile(clear_teal)["result"])

    # Create app (NO setup/deposit here)
    sp = client.suggested_params(); sp.flat_fee = True; sp.fee = MIN_FEE
    from time import time as _now
    # deadline far in future (not important for this test)
    deadline_round = client.status()["last-round"] + 1000
    create = txn.ApplicationCreateTxn(
        sender=creator_addr,
        sp=sp,
        on_complete=txn.OnComplete.NoOpOC.real,
        approval_program=ap,
        clear_program=cp,
        global_schema=txn.StateSchema(num_uints=8, num_byte_slices=2),
        local_schema=txn.StateSchema(num_uints=1, num_byte_slices=0),
        app_args=[
            decode_address(admin_addr),
            goal_micro.to_bytes(8, "big"),
            rate_per_algo.to_bytes(8, "big"),
            int(deadline_round).to_bytes(8, "big"),
        ],
    )
    stx = create.sign(creator_sk)
    txid = client.send_transaction(stx)
    ptx = robust_wait_for_confirmation(client, txid, 120)
    app_id = ptx["application-index"]
    app_addr = get_application_address(app_id)

    print(f"Created app {app_id} (no setup/deposit). App addr: {app_addr}")

    # Investor opts in (local state)
    sp = client.suggested_params()
    opt = txn.ApplicationOptInTxn(sender=inv1_addr, sp=sp, index=app_id).sign(inv1_sk)
    txid = client.send_transaction(opt)
    robust_wait_for_confirmation(client, txid, 120)
    print("Investor opted in to app.")

    # Attempt to contribute 1 ALGO without deposit/setup — must fail
    sp_call = client.suggested_params(); sp_call.flat_fee = True; sp_call.fee = MIN_FEE
    call = txn.ApplicationNoOpTxn(sender=inv1_addr, sp=sp_call, index=app_id, app_args=[b"contribute"])
    pay  = txn.PaymentTxn(sender=inv1_addr, sp=client.suggested_params(), receiver=app_addr, amt=1_000_000)
    gid = txn.calculate_group_id([call, pay]); call.group = gid; pay.group = gid

    try:
        client.send_transactions([call.sign(inv1_sk), pay.sign(inv1_sk)])
        # If the node accepted, wait and then fail the test — it SHOULD have been rejected.
        for t in [call.get_txid(), pay.get_txid()]:
            robust_wait_for_confirmation(client, t, 120)
        raise AssertionError("Contribution unexpectedly succeeded without deposit/setup")
    except AlgodHTTPError as e:
        print("Expected rejection received ✅")
        print(str(e))

    print("DEPOSIT GUARD TEST COMPLETE ✅")

if __name__ == "__main__":
    main()
