
import os
import time
from algosdk import account, mnemonic, encoding
from algosdk.v2client import algod
from algosdk.future import transaction
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer, AccountTransactionSigner, TransactionWithSigner
)
from algosdk.logic import get_application_address
from pyteal import compileTeal, Mode
from contracts.approval import approval
from contracts.clear import clear

ALGOD_ADDRESS = os.getenv("ALGOD_ADDRESS", "http://127.0.0.1:4001")
ALGOD_TOKEN = os.getenv("ALGOD_TOKEN", "a" * 64)

def get_algod():
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_ADDRESS)

def wait_for_tx(c, txid):
    last_round = c.status()["last-round"]
    while True:
        try:
            pending = c.pending_transaction_info(txid)
            if pending.get("confirmed-round", 0) > 0:
                return pending
            c.status_after_block(last_round + 1)
            last_round += 1
        except Exception:
            time.sleep(1)

def compile_program(client, teal):
    # Use algod v2 compile endpoint
    res = client.compile(teal)
    return res["hash"], res["result"]

def make_asset(client, creator_sk, total=10_000, decimals=0, unit="TOK", name="ProjectToken"):
    params = client.suggested_params()
    addr = account.address_from_private_key(creator_sk)
    txn = transaction.AssetCreateTxn(
        sender=addr,
        sp=params,
        total=total,
        default_frozen=False,
        unit_name=unit,
        asset_name=name,
        manager=addr,
        reserve=addr,
        freeze=None,
        clawback=None,
        decimals=decimals,
    )
    stx = txn.sign(creator_sk)
    txid = client.send_transaction(stx)
    resp = wait_for_tx(client, txid)
    return resp["asset-index"]

def fund(client, from_sk, to_addr, amt):
    params = client.suggested_params()
    txn = transaction.PaymentTxn(
        sender=account.address_from_private_key(from_sk),
        sp=params,
        receiver=to_addr,
        amt=amt,
    )
    stx = txn.sign(from_sk)
    txid = client.send_transaction(stx)
    wait_for_tx(client, txid)

def optin_app(client, sk, app_id):
    params = client.suggested_params()
    txn = transaction.ApplicationOptInTxn(
        sender=account.address_from_private_key(sk),
        sp=params,
        index=app_id
    )
    stx = txn.sign(sk)
    txid = client.send_transaction(stx)
    wait_for_tx(client, txid)

def send_group(client, txns):
    gid = transaction.calculate_group_id(txns)
    for t in txns:
        t.group = gid
    stxns = [t.sign(txn_info["sk"]) if isinstance(t, transaction.Transaction) else t for txn_info, t in []]

def main():
    client = get_algod()

    # Create simple funded accounts (replace with your keys or KMD usage)
    # For demo, we'll generate accounts and expect a local dispenser or existing funded account
    dev_sk, dev_addr = account.generate_account()
    admin_sk, admin_addr = account.generate_account()
    inv1_sk, inv1_addr = account.generate_account()
    inv2_sk, inv2_addr = account.generate_account()
    funder_sk, funder_addr = account.generate_account()

    print("Generated accounts:")
    print("Admin:", admin_addr)
    print("Dev  :", dev_addr)
    print("Inv1 :", inv1_addr)
    print("Inv2 :", inv2_addr)

    # *** IMPORTANT ***
    # Manually fund the accounts above (admin/dev/inv1/inv2) from your local faucet before running further.
    # Or uncomment below and fund from a pre-funded account you control.
    # Example:
    # fund(client, YOUR_FUNDED_SK, admin_addr, 5_000_000)
    # fund(client, YOUR_FUNDED_SK, dev_addr, 10_000_000)
    # fund(client, YOUR_FUNDED_SK, inv1_addr, 10_000_000)
    # fund(client, YOUR_FUNDED_SK, inv2_addr, 10_000_000)

    # Create ASA by developer (so dev holds supply)
    # Ensure developer is funded first.
    asa_id = make_asset(client, dev_sk, total=1_000_000, decimals=0, unit="TOK", name="ProjectToken")
    print("Created ASA:", asa_id)

    # Compile program
    approval_teal = compileTeal(approval(), Mode.Application, version=8)
    clear_teal = compileTeal(clear(), Mode.Application, version=8)
    app_hash, app_prog = compile_program(client, approval_teal)
    clr_hash, clr_prog = compile_program(client, clear_teal)

    # Parameters
    goal = 10_000_000  # 10 ALGO
    rate = 5          # 5 tokens per 1 ALGO
    duration_secs = 60 * 24 * 60 * 60  # 60 days
    deposit = goal * 2 // 100
    required_pool = goal * rate // 1_000_000

    sp = client.suggested_params()

    # Group: [0] deposit payment dev->app, [1] app create
    # We don't know app id yet, but app address is derived from future app id;
    # During create, contract checks Gtxn[0] receiver equals Global.current_application_address(),
    # which is allowed.
    # For SDK we must set receiver now: it's escrow generated from future app id; we cannot compute it.
    # Workaround: group order MUST be [0] Payment with receiver set to the **temp** app address after creation.
    # SDK cannot compute it pre-creation, so we will do a 2-step method:
    # 1) Create app first without enforcing deposit (skip check) -> Not possible with current logic.
    # => In production, use an **OnComplete: NoOp create** via AtomicTransactionComposer with method args,
    #    but setting receiver to "app address" pre-creation is supported by protocol; SDKs allow using
    #    `get_application_address(0)` is invalid pre-create.
    #
    # To keep this example simple and runnable, we perform:
    #  - Create the app with deposit check disabled in code OR
    #  - After create, send the deposit and assert in a separate endpoint.
    #
    # For now we demonstrate the *rest* of flow assuming deposit exists.
    #
    # >>> Replace this section with a proper ATC-based create flow in your integration. <<<

    # Create the application (without the deposit group due to SDK limitation in this minimal example)
    txn = transaction.ApplicationCreateTxn(
        sender=dev_addr,
        sp=sp,
        on_complete=transaction.OnComplete.NoOpOC,
        approval_program=bytes.fromhex(app_prog),
        clear_program=bytes.fromhex(clr_prog),
        global_schema=transaction.StateSchema(num_uints=8, num_byte_slices=2),
        local_schema=transaction.StateSchema(num_uints=2, num_byte_slices=0),
        app_args=[
            encoding.decode_address(admin_addr),
            encoding.decode_address(dev_addr),
            goal.to_bytes(8, "big"),
            duration_secs.to_bytes(8, "big"),
            asa_id.to_bytes(8, "big"),
            rate.to_bytes(8, "big"),
        ],
    )
    stx = txn.sign(dev_sk)
    txid = client.send_transaction(stx)
    result = wait_for_tx(client, txid)
    app_id = result["application-index"]
    app_addr = get_application_address(app_id)
    print("App ID:", app_id)
    print("App Addr:", app_addr)

    # Send deposit to app address now (in production, group with create as required by the contract)
    fund(client, dev_sk, app_addr, deposit + 300_000)  # extra for min bal

    # Bootstrap group: [0] app call bootstrap, [1] axfer tokens to app
    sp = client.suggested_params()
    call_boot = transaction.ApplicationNoOpTxn(
        sender=dev_addr,
        sp=sp,
        index=app_id,
        app_args=[b"bootstrap"],
        fee=sp.min_fee * 1,  # fee pooling for 1 inner tx
    )
    axfer = transaction.AssetTransferTxn(
        sender=dev_addr,
        sp=sp,
        receiver=app_addr,
        amt=required_pool,
        index=asa_id,
    )
    gid = transaction.calculate_group_id([call_boot, axfer])
    call_boot.group = gid
    axfer.group = gid
    stx1 = call_boot.sign(dev_sk)
    stx2 = axfer.sign(dev_sk)
    client.send_transactions([stx1, stx2])
    wait_for_tx(client, stx1.get_txid())

    # Investor opt-in
    optin_app(client, inv1_sk, app_id)
    optin_app(client, inv2_sk, app_id)

    # Contribute inv1 (6 ALGO) and inv2 (4 ALGO)
    def contribute(sk, addr, amount):
        sp = client.suggested_params()
        pay = transaction.PaymentTxn(sender=addr, sp=sp, receiver=app_addr, amt=amount)
        call = transaction.ApplicationNoOpTxn(sender=addr, sp=sp, index=app_id, app_args=[b"contribute"])
        gid = transaction.calculate_group_id([pay, call])
        pay.group = gid
        call.group = gid
        stx1 = pay.sign(sk)
        stx2 = call.sign(sk)
        client.send_transactions([stx1, stx2])
        wait_for_tx(client, stx2.get_txid())

    contribute(inv1_sk, inv1_addr, 6_000_000)
    contribute(inv2_sk, inv2_addr, 4_000_000)

    # Finalize success
    sp = client.suggested_params()
    finalize = transaction.ApplicationNoOpTxn(
        sender=dev_addr, sp=sp, index=app_id, app_args=[b"finalize_success"], fee=sp.min_fee * 2
    )
    stx = finalize.sign(dev_sk)
    client.send_transaction(stx)
    wait_for_tx(client, stx.get_txid())

    # Verify balances (rudimentary)
    ai = client.account_info(admin_addr)
    di = client.account_info(dev_addr)
    print("Admin balance:", ai["amount"])
    print("Dev balance  :", di["amount"])

    # Claims
    def claim(sk, addr):
        sp = client.suggested_params()
        call = transaction.ApplicationNoOpTxn(sender=addr, sp=sp, index=app_id, app_args=[b"claim"], fee=sp.min_fee * 1)
        stx = call.sign(sk)
        client.send_transaction(stx)
        wait_for_tx(client, stx.get_txid())

    claim(inv1_sk, inv1_addr)
    claim(inv2_sk, inv2_addr)

    print("Claims executed. Check ASA balances in your node.")

if __name__ == "__main__":
    main()
