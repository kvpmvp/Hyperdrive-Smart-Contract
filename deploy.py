from dataclasses import dataclass
from typing import Tuple

import base64
import os
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn
from algosdk.error import AlgodHTTPError
from algosdk.logic import get_application_address
from algosdk.encoding import decode_address

from pyteal import compileTeal, Mode
from crowdfunding_contract import approval_program, clear_program

# ---- Constants ----
MIN_FEE = 1000  # µAlgos

# ---- Config ----

@dataclass
class ProjectConfig:
    goal_algos: int        # e.g., 10 (ALGO)
    rate_per_algo: int     # e.g., 100 (tokens per ALGO)
    days_duration: int     # e.g., 60 (days until deadline)


# ---- Clients ----

def get_clients() -> Tuple[algod.AlgodClient, None]:
    algod_token = os.environ.get("ALGOD_TOKEN", "")
    algod_address = os.environ.get("ALGOD_URL", "https://testnet-api.algonode.cloud")
    headers = {"X-API-Key": algod_token} if algod_token else {}
    algod_client = algod.AlgodClient(algod_token, algod_address, headers)
    return algod_client, None


# ---- Robust waiter ----

def wait_for_confirmation(client: algod.AlgodClient, txid: str, timeout: int = 120):
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


def send_and_wait(client: algod.AlgodClient, signed):
    if isinstance(signed, list):
        groups = {s.transaction.group for s in signed}
        if len(groups) == 1 and list(groups)[0]:
            txids = [s.get_txid() for s in signed]
            client.send_transactions(signed)
            for tid in txids:
                wait_for_confirmation(client, tid)
            return txids
        txids = []
        for s in signed:
            txid = client.send_transaction(s)
            wait_for_confirmation(client, txid)
            txids.append(txid)
        return txids
    else:
        txid = client.send_transaction(signed)
        wait_for_confirmation(client, txid)
        return [txid]


def compile_program_bytes(algod_client: algod.AlgodClient, teal_source: str) -> bytes:
    compile_response = algod_client.compile(teal_source)
    return base64.b64decode(compile_response["result"])


def current_round(algod_client: algod.AlgodClient) -> int:
    return algod_client.status()["last-round"]


# ---- Asset + App creation ----

def create_asset(algod_client: algod.AlgodClient, creator_sk: str,
                 total: int, unit_name: str, asset_name: str, decimals: int = 0) -> int:
    creator_addr = account.address_from_private_key(creator_sk)
    sp = algod_client.suggested_params()
    sp.flat_fee = True
    sp.fee = MIN_FEE

    atxn = txn.AssetCreateTxn(
        sender=creator_addr,
        sp=sp,
        total=total,
        default_frozen=False,
        unit_name=unit_name,
        asset_name=asset_name,
        manager=creator_addr,
        reserve=creator_addr,
        freeze=None,
        clawback=None,
        decimals=decimals,
    )
    stx = atxn.sign(creator_sk)
    txid = algod_client.send_transaction(stx)
    ptx = wait_for_confirmation(algod_client, txid, timeout=120)
    asa_id = ptx.get("asset-index")
    if asa_id is None:
        raise RuntimeError("ASA creation failed; no asset-index in pending tx info")
    return asa_id


def deploy_crowdfund(creator_mn: str, admin_mn: str, cfg: ProjectConfig):
    algod_client, _ = get_clients()

    creator_sk = mnemonic.to_private_key(creator_mn)
    creator_addr = account.address_from_private_key(creator_sk)
    admin_sk = mnemonic.to_private_key(admin_mn)
    admin_addr = account.address_from_private_key(admin_sk)

    # Compute values
    goal_micro = cfg.goal_algos * 1_000_000
    tokens_expected = cfg.goal_algos * cfg.rate_per_algo
    deposit_amt = (goal_micro * 2) // 100  # 2%

    # Deadline as round height (generous)
    rounds_per_day = 20_000
    deadline_round = current_round(algod_client) + cfg.days_duration * rounds_per_day

    # Compile programs
    approval_teal = compileTeal(approval_program(), mode=Mode.Application, version=8)
    clear_teal    = compileTeal(clear_program(),     mode=Mode.Application, version=8)
    approval_prog = compile_program_bytes(algod_client, approval_teal)
    clear_prog    = compile_program_bytes(algod_client, clear_teal)

    # Create the app
    sp_app = algod_client.suggested_params()
    sp_app.flat_fee = True
    sp_app.fee = MIN_FEE

    app_args = [
        decode_address(admin_addr),                # bytes (admin address)
        goal_micro.to_bytes(8, "big"),
        cfg.rate_per_algo.to_bytes(8, "big"),
        int(deadline_round).to_bytes(8, "big"),
    ]

    create_txn = txn.ApplicationCreateTxn(
        sender=creator_addr,
        sp=sp_app,
        on_complete=txn.OnComplete.NoOpOC.real,
        approval_program=approval_prog,
        clear_program=clear_prog,
        # CHANGED: 9 uints (goal, rate, deadline, asa_id, raised, deposit, funded, fee_paid, contrib_count)
        global_schema=txn.StateSchema(num_uints=9, num_byte_slices=2),
        local_schema=txn.StateSchema(num_uints=1, num_byte_slices=0),
        app_args=app_args,
    )
    stx_create = create_txn.sign(creator_sk)
    txid = algod_client.send_transaction(stx_create)
    ptx = wait_for_confirmation(algod_client, txid, timeout=120)
    app_id = ptx["application-index"]
    app_addr = get_application_address(app_id)

    # Create ASA
    asa_id = create_asset(
        algod_client, creator_sk,
        total=tokens_expected,
        unit_name="PRJ",
        asset_name="ProjectToken",
        decimals=0,
    )

    # Setup group: deposit 2% -> AppCall("setup", app_args=["setup", asa_id]) -> ASA transfer seed->app
    sp_pay = algod_client.suggested_params()
    sp_pay.flat_fee = True
    sp_pay.fee = MIN_FEE

    pay_deposit = txn.PaymentTxn(
        sender=creator_addr,
        sp=sp_pay,
        receiver=app_addr,
        amt=deposit_amt,
    )

    sp_setup = algod_client.suggested_params()
    sp_setup.flat_fee = True
    sp_setup.fee = MIN_FEE * 2  # inner ASA opt-in

    setup_call = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=sp_setup,
        index=app_id,
        app_args=[b"setup", int(asa_id).to_bytes(8, "big")],  # pass ASA id explicitly
        foreign_assets=[asa_id],  # <-- REQUIRED so inner itxn can reference the ASA
    )

    sp_xfer = algod_client.suggested_params()
    sp_xfer.flat_fee = True
    sp_xfer.fee = MIN_FEE

    seed_tokens = txn.AssetTransferTxn(
        sender=creator_addr,
        sp=sp_xfer,
        index=asa_id,
        receiver=app_addr,
        amt=tokens_expected,  # >= (goal * rate) / 1e6
    )

    gid = txn.calculate_group_id([pay_deposit, setup_call, seed_tokens])
    pay_deposit.group = gid
    setup_call.group  = gid
    seed_tokens.group = gid

    stx1 = pay_deposit.sign(creator_sk)
    stx2 = setup_call.sign(creator_sk)
    stx3 = seed_tokens.sign(creator_sk)

    send_and_wait(algod_client, [stx1, stx2, stx3])

    print("Setup complete: deposit received & ASA seeded (app opted-in).")
    return app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt


if __name__ == "__main__":
    CREATOR_MN = os.environ.get("CREATOR_MN", "")
    ADMIN_MN   = os.environ.get("ADMIN_MN", "")
    if not CREATOR_MN or not ADMIN_MN:
        print("Set CREATOR_MN and ADMIN_MN in env to run this directly.")
    else:
        cfg = ProjectConfig(goal_algos=10, rate_per_algo=100, days_duration=60)
        res = deploy_crowdfund(CREATOR_MN, ADMIN_MN, cfg)
        print("Deployed:", res)
