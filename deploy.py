import base64
from dataclasses import dataclass
from typing import Tuple, List

from algosdk import account, mnemonic, encoding
from algosdk.v2client import algod, indexer
from algosdk import transaction as txn
from algosdk.logic import get_application_address
from algosdk.error import AlgodHTTPError

from pyteal import compileTeal, Mode
from crowdfunding_contract import approval_program, clear_program

ALGOD_ADDRESS = "https://testnet-api.algonode.cloud"
INDEXER_ADDRESS = "https://testnet-idx.algonode.cloud"
ALGOD_TOKEN = ""   # Algonode public; no token
INDEXER_TOKEN = ""

MIN_FEE = 1000

def get_clients() -> Tuple[algod.AlgodClient, indexer.IndexerClient]:
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_ADDRESS), indexer.IndexerClient(INDEXER_TOKEN, INDEXER_ADDRESS)

def wait_for_confirmation(client: algod.AlgodClient, txid: str, timeout: int = 60):
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
            # If confirmed
            cr = p.get("confirmed-round", 0)
            if cr and cr > 0:
                return p
            # If still pending, fall through to wait next round
        except AlgodHTTPError as e:
            # Common while node hasn't seen the tx yet; ignore and keep waiting
            pass

        # Advance a round and check timeout
        last_round += 1
        client.status_after_block(last_round)
        if time.time() - start > timeout:
            raise TimeoutError(f"Transaction {txid} not confirmed after {timeout}s")

def compile_program(client: algod.AlgodClient, teal_source: str) -> bytes:
    response = client.compile(teal_source)
    return base64.b64decode(response["result"])

def teal_sources():
    appr_teal = compileTeal(approval_program(), Mode.Application, version=8)
    clear_teal = compileTeal(clear_program(), Mode.Application, version=8)
    return appr_teal, clear_teal

def create_asset(algod_client: algod.AlgodClient, creator_sk: str, total: int, unit_name: str, asset_name: str, decimals: int = 0) -> int:
    params = algod_client.suggested_params()
    txn_tx = txn.AssetCreateTxn(
        sender=account.address_from_private_key(creator_sk),
        sp=params,
        total=total,
        default_frozen=False,
        unit_name=unit_name,
        asset_name=asset_name,
        manager=account.address_from_private_key(creator_sk),
        reserve=None,
        freeze=None,
        clawback=None,
        url="",
        decimals=decimals,
    )
    stx = txn_tx.sign(creator_sk)
    txid = algod_client.send_transaction(stx)
    ptx = wait_for_confirmation(algod_client, txid, timeout=60)
    return ptx["asset-index"]

@dataclass
class ProjectConfig:
    goal_algos: int
    rate_per_algo: int
    days_duration: int

def approx_rounds(days: int) -> int:
    seconds = days * 24 * 60 * 60
    return int(seconds / 3.7)

def deploy_crowdfund(creator_mn: str, admin_mn: str, cfg: ProjectConfig):
    algod_client, _ = get_clients()

    creator_sk = mnemonic.to_private_key(creator_mn)
    creator_addr = account.address_from_private_key(creator_sk)
    admin_sk = mnemonic.to_private_key(admin_mn)  # not used directly below; validates format
    admin_addr = account.address_from_private_key(admin_sk)

    # 1) Create ASA (developer token) sized to rate * goal
    total_tokens = cfg.rate_per_algo * cfg.goal_algos
    asa_id = create_asset(algod_client, creator_sk, total=total_tokens, unit_name="PRJ", asset_name="ProjectToken", decimals=0)
    print(f"Created ASA {asa_id} total={total_tokens} (decimals=0)")

    # 2) Compile programs
    appr_teal, clear_teal = teal_sources()
    appr_bin = compile_program(algod_client, appr_teal)
    clear_bin = compile_program(algod_client, clear_teal)

    # 3) Create application
    params = algod_client.suggested_params()
    goal_micro = cfg.goal_algos * 1_000_000
    deadline_delta = approx_rounds(cfg.days_duration)

    app_args = [
        encoding.decode_address(admin_addr),
        goal_micro.to_bytes(8, "big"),
        cfg.rate_per_algo.to_bytes(8, "big"),
        (algod_client.status()["last-round"] + deadline_delta).to_bytes(8, "big"),
    ]

    global_schema = txn.StateSchema(num_uints=6, num_byte_slices=2)  # goal, rate, deadline, asa_id, raised, deposit; creator/admin
    local_schema = txn.StateSchema(num_uints=1, num_byte_slices=0)   # contrib

    app_create = txn.ApplicationCreateTxn(
        sender=creator_addr,
        sp=params,
        on_complete=txn.OnComplete.NoOpOC,
        approval_program=appr_bin,
        clear_program=clear_bin,
        global_schema=global_schema,
        local_schema=local_schema,
        app_args=app_args,
    )
    stx = app_create.sign(creator_sk)
    txid = algod_client.send_transaction(stx)
    ptx = wait_for_confirmation(algod_client, txid, timeout=60)
    app_id = ptx["application-index"]
    app_addr = get_application_address(app_id)
    print(f"Created App ID {app_id}, address {app_addr}")

    # 4) Setup group (deposit -> appcall(setup) -> axfer seed)
    deposit_amt = goal_micro * 2 // 100  # 2%
    tokens_expected = goal_micro * cfg.rate_per_algo // 1_000_000

    sp_pay = algod_client.suggested_params()
    sp_call = algod_client.suggested_params()
    sp_axfer = algod_client.suggested_params()

    # Budget for inner ASA opt-in during AppCall
    sp_call.flat_fee = True
    sp_call.fee = MIN_FEE * 3   # 3k to be generous (outer + inner)

    pay_deposit = txn.PaymentTxn(
        sender=creator_addr,
        sp=sp_pay,
        receiver=app_addr,
        amt=deposit_amt
    )

    call_setup = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=sp_call,
        index=app_id,
        app_args=[b"setup"],
        foreign_assets=[asa_id],
    )

    axfer_seed = txn.AssetTransferTxn(
        sender=creator_addr,
        sp=sp_axfer,
        index=asa_id,
        receiver=app_addr,
        amt=tokens_expected
    )

    gid = txn.calculate_group_id([pay_deposit, call_setup, axfer_seed])
    pay_deposit.group = gid
    call_setup.group = gid
    axfer_seed.group = gid

    stxs = [
        pay_deposit.sign(creator_sk),
        call_setup.sign(creator_sk),
        axfer_seed.sign(creator_sk),
    ]

    # Send and wait on all txids (especially the last one from the group)
    txids: List[str] = [s.get_txid() for s in stxs]
    algod_client.send_transactions(stxs)
    for tid in txids:
        wait_for_confirmation(algod_client, tid, timeout=90)
    print("Setup complete: deposit received & ASA seeded (app opted-in).")

    return app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt

if __name__ == "__main__":
    print("Use test_funded.py to run the scenario.")
