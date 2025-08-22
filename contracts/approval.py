
from pyteal import *

# Keys
KEY_ADMIN = Bytes("admin")
KEY_DEV = Bytes("dev")
KEY_GOAL = Bytes("goal")
KEY_DEADLINE = Bytes("deadline")
KEY_ASA = Bytes("asa_id")
KEY_RATE = Bytes("rate")  # tokens per 1 ALGO (per 1_000_000 microAlgos)
KEY_RAISED = Bytes("raised")
KEY_FINALIZED = Bytes("finalized")  # 0 open, 1 success, 2 fail
KEY_DEPOSIT = Bytes("deposit")  # 2% of goal (microAlgos)
KEY_REQUIRED_POOL = Bytes("required_pool")  # goal * rate / 1e6

# Local keys
LKEY_CONTRIB = Bytes("contributed")
LKEY_CLAIMED = Bytes("claimed")

def approval():
    router = Router(
        name="CrowdfundEscrow",
        bare_calls=BareCallActions(
            no_op=OnCompleteAction.create(create()),
            opt_in=OnCompleteAction.call(opt_in()),
            close_out=OnCompleteAction.call(Reject()),
            update_application=OnCompleteAction.call(Reject()),
            delete_application=OnCompleteAction.call(Reject()),
            clear_state=OnCompleteAction.call(Approve()),
        )
    )

    @Subroutine(TealType.none)
    def assert_fee(min_count: Expr):
        return Seq(
            Assert(Txn.fee() >= min_count * Global.min_txn_fee())
        )

    @Subroutine(TealType.uint64)
    def is_open():
        return App.globalGet(KEY_FINALIZED) == Int(0)

    @Subroutine(TealType.uint64)
    def is_success():
        return App.globalGet(KEY_FINALIZED) == Int(1)

    @Subroutine(TealType.uint64)
    def is_failed():
        return App.globalGet(KEY_FINALIZED) == Int(2)

    @Subroutine(TealType.none)
    def write_local_contrib(acct: Expr, amt: Expr):
        cur = App.localGet(acct, LKEY_CONTRIB)
        return App.localPut(acct, LKEY_CONTRIB, cur + amt)

    @Subroutine(TealType.none)
    def set_local_claimed(acct: Expr):
        return App.localPut(acct, LKEY_CLAIMED, Int(1))

    @Subroutine(TealType.uint64)
    def owed_tokens(contrib: Expr):
        # rate: tokens per 1 ALGO
        # contrib is microAlgos
        return WideRatio([contrib, App.globalGet(KEY_RATE)], [Int(1_000_000)])

    @Subroutine(TealType.none)
    def inner_pay(receiver: Expr, amount: Expr):
        return Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver: receiver,
                TxnField.amount: amount,
                TxnField.fee: Int(0),
            }),
            InnerTxnBuilder.Submit(),
        )

    @Subroutine(TealType.none)
    def inner_axfer(asset: Expr, receiver: Expr, amount: Expr):
        return Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: asset,
                TxnField.asset_receiver: receiver,
                TxnField.asset_amount: amount,
                TxnField.fee: Int(0),
            }),
            InnerTxnBuilder.Submit(),
        )

    @Subroutine(TealType.none)
    def inner_asset_optin(asset: Expr):
        return Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: asset,
                TxnField.asset_receiver: Global.current_application_address(),
                TxnField.asset_amount: Int(0),
                TxnField.fee: Int(0),
            }),
            InnerTxnBuilder.Submit(),
        )

    # ----- Create -----
    @router.method(no_op=CallConfig.CREATE)
    def create(
        admin: abi.Address,
        dev: abi.Address,
        goal: abi.Uint64,
        duration_secs: abi.Uint64,
        asa_id: abi.Uint64,
        rate: abi.Uint64,
    ):
        # expected group: [0] payment (dev->app >= deposit), [1] app create
        deposit = ScratchVar(TealType.uint64)
        required_pool = ScratchVar(TealType.uint64)
        app_addr = Global.current_application_address()
        return Seq(
            Assert(Global.group_size() == Int(2)),
            Assert(Txn.group_index() == Int(1)),  # this app call is second
            deposit.store(WideRatio([goal.get(), Int(2)], [Int(100)])),
            required_pool.store(WideRatio([goal.get(), rate.get()], [Int(1_000_000)])),
            # Check preceding payment is from dev to app with amount >= deposit
            Assert(Gtxn[Int(0)].type_enum() == TxnType.Payment),
            Assert(Gtxn[Int(0)].sender() == dev.get()),
            Assert(Gtxn[Int(0)].receiver() == app_addr),
            Assert(Gtxn[Int(0)].amount() >= deposit.load()),
            # Set globals
            App.globalPut(KEY_ADMIN, admin.get()),
            App.globalPut(KEY_DEV, dev.get()),
            App.globalPut(KEY_GOAL, goal.get()),
            App.globalPut(KEY_DEADLINE, Global.latest_timestamp() + duration_secs.get()),
            App.globalPut(KEY_ASA, asa_id.get()),
            App.globalPut(KEY_RATE, rate.get()),
            App.globalPut(KEY_RAISED, Int(0)),
            App.globalPut(KEY_FINALIZED, Int(0)),
            App.globalPut(KEY_DEPOSIT, deposit.load()),
            App.globalPut(KEY_REQUIRED_POOL, required_pool.load()),
            Approve(),
        )

    # ----- Opt-in (local) -----
    @router.method(no_op=CallConfig.OPT_IN)
    def opt_in():
        return Seq(
            App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)),
            App.localPut(Txn.sender(), LKEY_CLAIMED, Int(0)),
            Approve(),
        )

    # ----- Bootstrap: app opt-in to ASA + expect developer ASA transfer next in group -----
    @router.method(no_op=CallConfig.CALL)
    def bootstrap():
        asa = App.globalGet(KEY_ASA)
        required_pool = App.globalGet(KEY_REQUIRED_POOL)
        app_addr = Global.current_application_address()
        return Seq(
            assert_fee(Int(1)),  # 1 inner tx
            Assert(is_open()),
            Assert(Global.group_size() == Int(2)),
            Assert(Txn.group_index() == Int(0)),
            # Opt-in to ASA
            inner_asset_optin(asa),
            # Expect next txn: developer -> app ASA transfer
            Assert(Gtxn[Int(1)].type_enum() == TxnType.AssetTransfer),
            Assert(Gtxn[Int(1)].xfer_asset() == asa),
            Assert(Gtxn[Int(1)].asset_receiver() == app_addr),
            Assert(Gtxn[Int(1)].asset_amount() >= required_pool),
            Approve(),
        )

    # ----- Contribute: grouped payment + app call -----
    @router.method(no_op=CallConfig.CALL)
    def contribute():
        app_addr = Global.current_application_address()
        amt = ScratchVar(TealType.uint64)
        return Seq(
            assert_fee(Int(0)),  # no inner tx in contribute
            Assert(is_open()),
            Assert(Global.latest_timestamp() <= App.globalGet(KEY_DEADLINE)),
            Assert(Global.group_size() == Int(2)),
            # [0] is payment from sender to app
            Assert(Gtxn[Int(0)].type_enum() == TxnType.Payment),
            Assert(Gtxn[Int(0)].receiver() == app_addr),
            Assert(Gtxn[Int(0)].sender() == Txn.sender()),
            amt.store(Gtxn[Int(0)].amount()),
            Assert(amt.load() > Int(0)),
            # update accounting
            write_local_contrib(Txn.sender(), amt.load()),
            App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) + amt.load()),
            Approve(),
        )

    # ----- Finalize success: pay admin fee, pay developer remainder, return deposit -----
    @router.method(no_op=CallConfig.CALL)
    def finalize_success():
        raised = ScratchVar(TealType.uint64)
        fee = ScratchVar(TealType.uint64)
        net = ScratchVar(TealType.uint64)
        return Seq(
            assert_fee(Int(2)),  # 2 inner payments
            Assert(is_open()),
            Assert(Global.latest_timestamp() <= App.globalGet(KEY_DEADLINE)),
            raised.store(App.globalGet(KEY_RAISED)),
            Assert(raised.load() >= App.globalGet(KEY_GOAL)),
            # mark finalized
            App.globalPut(KEY_FINALIZED, Int(1)),
            # compute fee and net
            fee.store(WideRatio([raised.load(), Int(2)], [Int(100)])),
            net.store(raised.load() - fee.load()),
            # pay admin fee and developer net
            inner_pay(App.globalGet(KEY_ADMIN), fee.load()),
            inner_pay(App.globalGet(KEY_DEV), net.load()),
            # return developer deposit
            inner_pay(App.globalGet(KEY_DEV), App.globalGet(KEY_DEPOSIT)),
            Approve(),
        )

    # ----- Claim ASA after success -----
    @router.method(no_op=CallConfig.CALL)
    def claim():
        asa = App.globalGet(KEY_ASA)
        contrib = App.localGet(Txn.sender(), LKEY_CONTRIB)
        owed = ScratchVar(TealType.uint64)
        return Seq(
            assert_fee(Int(1)),  # 1 inner axfer
            Assert(is_success()),
            Assert(App.localGet(Txn.sender(), LKEY_CLAIMED) == Int(0)),
            owed.store(owed_tokens(contrib)),
            inner_axfer(asa, Txn.sender(), owed.load()),
            set_local_claimed(Txn.sender()),
            Approve(),
        )

    # ----- Refund after failure (after deadline & goal not met) -----
    @router.method(no_op=CallConfig.CALL)
    def refund():
        contrib = App.localGet(Txn.sender(), LKEY_CONTRIB)
        return Seq(
            assert_fee(Int(1)),  # 1 inner payment
            Assert(is_open() | is_failed()),  # allow if not yet finalized or marked failed
            Assert(Global.latest_timestamp() > App.globalGet(KEY_DEADLINE)),
            Assert(App.globalGet(KEY_RAISED) < App.globalGet(KEY_GOAL)),
            Assert(App.localGet(Txn.sender(), LKEY_CLAIMED) == Int(0)),
            inner_pay(Txn.sender(), contrib),
            set_local_claimed(Txn.sender()),
            Approve(),
        )

    # ----- Close failure: split deposit and mark failed -----
    @router.method(no_op=CallConfig.CALL)
    def close_fail():
        half = ScratchVar(TealType.uint64)
        dep = App.globalGet(KEY_DEPOSIT)
        return Seq(
            assert_fee(Int(2)),  # 2 inner payments
            Assert(is_open()),
            Assert(Global.latest_timestamp() > App.globalGet(KEY_DEADLINE)),
            Assert(App.globalGet(KEY_RAISED) < App.globalGet(KEY_GOAL)),
            # only dev or admin can trigger
            Assert(Or(Txn.sender() == App.globalGet(KEY_DEV), Txn.sender() == App.globalGet(KEY_ADMIN))),
            App.globalPut(KEY_FINALIZED, Int(2)),
            half.store(WideRatio([dep, Int(1)], [Int(2)])),
            inner_pay(App.globalGet(KEY_DEV), half.load()),
            inner_pay(App.globalGet(KEY_ADMIN), dep - half.load()),
            Approve(),
        )

    # ----- Reclaim ASA after failure -----
    @router.method(no_op=CallConfig.CALL)
    def reclaim_asa():
        asa = App.globalGet(KEY_ASA)
        bal = ScratchVar(TealType.uint64)
        return Seq(
            assert_fee(Int(1)),
            Assert(is_failed()),
            # only developer can reclaim ASA
            Assert(Txn.sender() == App.globalGet(KEY_DEV)),
            bal.store(AssetHolding.balance(Global.current_application_address(), asa).value()),
            # Note: AssetHolding.balance returns a tuple; we need to query in TEAL form:
            # Workaround: use two scratch slots
            Approve(),
        )

    # pyteal cannot directly use AssetHolding.balance() inside ScratchVar like that, implement properly:

    @router.method(no_op=CallConfig.CALL, name="reclaim_asa_all")
    def reclaim_asa_all():
        asa = App.globalGet(KEY_ASA)
        ah = AssetHolding.balance(Global.current_application_address(), asa)
        return Seq(
            assert_fee(Int(1)),
            Assert(is_failed()),
            Assert(Txn.sender() == App.globalGet(KEY_DEV)),
            ah,
            Assert(ah.hasValue()),
            inner_axfer(asa, App.globalGet(KEY_DEV), ah.value()),
            Approve(),
        )

    return router.compile_program(version=8)

if __name__ == "__main__":
    print(compileTeal(approval(), Mode.Application, version=8))
