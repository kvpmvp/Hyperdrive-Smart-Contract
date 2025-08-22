"""
Verify developer payout on success:
- Deploy fresh app (goal=10 ALGO, rate=100)
- Contributions: 6 + 4 ALGO (== goal)
- Finalize
- Assert deltas:
  - Admin gets 2% of (goal + deposit)   <== matches current contract logic
  - Creator receives the remainder (goal + deposit - admin_fee),
    accounting for the creator's prior spend (ASA create, app create, setup fees).
"""

from typing import Tuple

from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction as txn

from deploy import get_clients, deploy_crowdfund, MIN_FEE, ProjectConfig

# ---- SAME MNEMONICS ----
CREATOR_MN = "able install flower toward cheap matter shallow switch dash roof suit eyebrow cheese current bleak enhance awesome brother leader they again simple desert about popular"
ADMIN_MN   = "science young voyage utility argue issue chase between dumb urban stone come hotel seat scorpion simple oak hub review gesture gossip smart city absent huge"
INV1_MN    = "damage cute radio venue palace stick double luggage round baby action fetch orchard pencil above slot water cement slot piano title gravity clutch absent sea"
INV2_MN    = "snow chat helmet surface enlist smile boss gesture region purse myth copper end link you trial sleep round vast tower farm tunnel humble able sentence"

def addr_from_mn(mn: str) -> Tuple[str, str]:
    sk = mnemonic.to_private_key(mn)
    addr = account.address_from_private_key(sk)
    return addr, sk

def wait_for_confirmation(client: algod.AlgodClient, txid: str, timeout: int = 90):
    import time
    from algosdk.error import AlgodHTTPError
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

def has_asset(client: algod.AlgodClient, addr: str, asa_id: int) -> bool:
    for a in client.account_info(addr).get("assets", []):
        if a.get("asset-id") == asa_id:
            return True
    return False

def opt_in_asset(client: algod.AlgodClient, addr: str, sk: str, asa_id: int):
    if has_asset(client, addr, asa_id):
        return
    sp = client.suggested_params()
    stx = txn.AssetTransferTxn(sender=addr, sp=sp, index=asa_id, receiver=addr, amt=0).sign(sk)
    txid = client.send_transaction(stx)
    wait_for_confirmation(client, txid, timeout=90)

def send_group_and_wait(client: algod.AlgodClient, txns):
    gid = txn.calculate_group_id(txns)
    for t in txns:
        t.group = gid
    stxs = [t.sign(sk) for t, sk in signed_pairs(txns)]
    txids = [s.get_txid() for s in stxs]
    client.send_transactions(stxs)
    for tid in txids:
        wait_for_confirmation(client, tid, timeout=90)

def signed_pairs(txns):
    # helper only used locally where we inline signers
    raise NotImplementedError

def main():
    algod_client, _ = get_clients()

    creator_addr, creator_sk = addr_from_mn(CREATOR_MN)
    admin_addr, admin_sk     = addr_from_mn(ADMIN_MN)
    inv1_addr, inv1_sk       = addr_from_mn(INV1_MN)
    inv2_addr, inv2_sk       = addr_from_mn(INV2_MN)

    # Baseline balances before everything
    c0 = get_algo(algod_client, creator_addr)
    a0 = get_algo(algod_client, admin_addr)

    # 1) Deploy
    cfg = ProjectConfig(goal_algos=10, rate_per_algo=100, days_duration=60)
    app_id, app_addr, asa_id, tokens_expected, goal_micro, deposit_amt = deploy_crowdfund(CREATOR_MN, ADMIN_MN, cfg)

    # 2) Investors: app opt-in
    sp = algod_client.suggested_params()
    stx1 = txn.ApplicationOptInTxn(sender=inv1_addr, sp=sp, index=app_id).sign(inv1_sk)
    txid = algod_client.send_transaction(stx1)
    wait_for_confirmation(algod_client, txid, 90)
    stx2 = txn.ApplicationOptInTxn(sender=inv2_addr, sp=algod_client.suggested_params(), index=app_id).sign(inv2_sk)
    txid = algod_client.send_transaction(stx2)
    wait_for_confirmation(algod_client, txid, 90)

    # 3) Investors: ASA opt-in (so they can receive tokens)
    opt_in_asset(algod_client, inv1_addr, inv1_sk, asa_id)
    opt_in_asset(algod_client, inv2_addr, inv2_sk, asa_id)

    # 4) Contributions
    def contribute(addr, sk, algos):
        p_app = algod_client.suggested_params()
        p_app.flat_fee = True
        p_app.fee = MIN_FEE
        call = txn.ApplicationNoOpTxn(sender=addr, sp=p_app, index=app_id, app_args=[b"contribute"])
        pay  = txn.PaymentTxn(sender=addr, sp=algod_client.suggested_params(), receiver=app_addr, amt=microalgos(algos))
        gid = txn.calculate_group_id([call, pay])
        call.group = gid
        pay.group = gid
        stxa = call.sign(sk); stxb = pay.sign(sk)
        algod_client.send_transactions([stxa, stxb])
        for tid in [stxa.get_txid(), stxb.get_txid()]:
            wait_for_confirmation(algod_client, tid, 90)

    contribute(inv1_addr, inv1_sk, 6)
    contribute(inv2_addr, inv2_sk, 4)

    # Snapshot balances before finalize
    c1 = get_algo(algod_client, creator_addr)
    a1 = get_algo(algod_client, admin_addr)
    app_pre = get_algo(algod_client, app_addr)  # should be goal + deposit (minus fees already spent by app, if any)

    # 5) Finalize (payout + close)
    p = algod_client.suggested_params()
    p.flat_fee = True
    p.fee = MIN_FEE * 6
    finalize = txn.ApplicationNoOpTxn(
        sender=creator_addr,
        sp=p,
        index=app_id,
        app_args=[b"finalize"],
        accounts=[inv1_addr, inv2_addr],
        foreign_assets=[asa_id],
    ).sign(creator_sk)
    txid = algod_client.send_transaction(finalize)
    wait_for_confirmation(algod_client, txid, 120)

    # Post-finalize balances
    c2 = get_algo(algod_client, creator_addr)
    a2 = get_algo(algod_client, admin_addr)
    app_post = get_algo(algod_client, app_addr)

    # EXPECTATIONS (per current contract)
    # admin_fee = 2% of app balance at finalize time
    # app balance should be roughly goal + deposit (ignoring tiny fee dust)
    admin_fee_expected = (app_pre * 2) // 100
    # remainder goes to creator (via close remainder)
    creator_gain_expected = app_pre - admin_fee_expected

    admin_gain = a2 - a1
    creator_gain = c2 - c1

    print("\n---- Payout Summary ----")
    print(f"App pre-finalize balance: {app_pre} µAlgos")
    print(f"Expected admin fee (2% of app_pre): {admin_fee_expected} µAlgos")
    print(f"Expected creator remainder: {creator_gain_expected} µAlgos")
    print(f"Observed admin gain: {admin_gain} µAlgos")
    print(f"Observed creator gain: {creator_gain} µAlgos")
    print(f"App post-finalize balance: {app_post} µAlgos (should be 0)")

    # Allow a small slack for network fee dust on outer/inner txns
    SLACK = 3_000  # µAlgos

    assert abs(admin_gain - admin_fee_expected) <= SLACK, "Admin fee does not match expectation"
    assert creator_gain >= (creator_gain_expected - 10_000), "Creator gain lower than expected (fees too high?)"
    assert app_post == 0, "App account should be closed (0 µAlgos)"

    print("✅ Developer payout verified.")

if __name__ == "__main__":
    main()
