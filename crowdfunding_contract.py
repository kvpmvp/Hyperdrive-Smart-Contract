from pyteal import *

# Crowdfunding App (stateful, smart-contract account)

KEY_GOAL     = Bytes("goal")
KEY_RATE     = Bytes("rate")
KEY_DEADLINE = Bytes("deadline")
KEY_ASA      = Bytes("asa_id")
KEY_RAISED   = Bytes("raised")
KEY_DEPOSIT  = Bytes("deposit")
KEY_CREATOR  = Bytes("creator")
KEY_ADMIN    = Bytes("admin")

LKEY_CONTRIB = Bytes("contrib")


def approval_program():
    on_create = Seq(
        Assert(Txn.application_args.length() == Int(4)),
        App.globalPut(KEY_CREATOR, Txn.sender()),
        App.globalPut(KEY_ADMIN,   Txn.application_args[0]),  # address bytes (kept for compatibility)
        App.globalPut(KEY_GOAL,    Btoi(Txn.application_args[1])),
        App.globalPut(KEY_RATE,    Btoi(Txn.application_args[2])),
        App.globalPut(KEY_DEADLINE,Btoi(Txn.application_args[3])),
        App.globalPut(KEY_RAISED,  Int(0)),
        App.globalPut(KEY_ASA,     Int(0)),
        App.globalPut(KEY_DEPOSIT, Int(0)),
        Approve(),
    )

    app_id   = Global.current_application_id()
    app_addr = Global.current_application_address()
    goal     = App.globalGet(KEY_GOAL)
    rate     = App.globalGet(KEY_RATE)
    deadline = App.globalGet(KEY_DEADLINE)
    asa_id   = App.globalGet(KEY_ASA)
    deposit  = App.globalGet(KEY_DEPOSIT)
    creator  = App.globalGet(KEY_CREATOR)
    # admin is no longer used in logic, but retained in state for compatibility
    admin    = App.globalGet(KEY_ADMIN)

    is_creator      = Txn.sender() == creator
    before_deadline = Global.round() <= deadline
    after_deadline  = Global.round() >  deadline

    # setup: EXPECT group = [0] Payment(deposit -> app), [1] AppCall("setup"), [2] AssetTransfer(seed ASA -> app)
    expected_deposit = (goal * Int(2)) / Int(100)
    setup = Seq(
        Assert(is_creator),
        Assert(Txn.assets.length() == Int(1)),
        App.globalPut(KEY_ASA, Txn.assets[0]),
        Assert(Global.group_size() >= Int(3)),

        # deposit first
        Assert(Gtxn[0].type_enum() == TxnType.Payment),
        Assert(Gtxn[0].sender()    == Txn.sender()),
        Assert(Gtxn[0].receiver()  == app_addr),
        Assert(Gtxn[0].amount()    == expected_deposit),
        App.globalPut(KEY_DEPOSIT, Gtxn[0].amount()),

        # inner opt-in to ASA (now we have balance for min-balance)
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields({
            TxnField.type_enum:      TxnType.AssetTransfer,
            TxnField.xfer_asset:     Txn.assets[0],
            TxnField.asset_receiver: app_addr,
            TxnField.asset_amount:   Int(0),
        }),
        InnerTxnBuilder.Submit(),

        # seed tokens
        Assert(Gtxn[2].type_enum()      == TxnType.AssetTransfer),
        Assert(Gtxn[2].sender()         == Txn.sender()),
        Assert(Gtxn[2].asset_receiver() == app_addr),
        Assert(Gtxn[2].xfer_asset()     == Txn.assets[0]),
        Assert(Gtxn[2].asset_amount()   >= (goal * rate) / Int(1_000_000)),
        Approve()
    )

    # contribute: grouped [AppCall("contribute"), Payment(sender -> app)]
    investor = Txn.sender()
    contribute = Seq(
        Assert(before_deadline),
        Assert(Global.group_size() >= Int(2)),
        Assert(Gtxn[1].type_enum() == TxnType.Payment),
        Assert(Gtxn[1].sender()    == investor),
        Assert(Gtxn[1].receiver()  == app_addr),
        Assert(Gtxn[1].amount()    >  Int(0)),
        # cap: no oversubscription
        Assert(App.globalGet(KEY_RAISED) + Gtxn[1].amount() <= goal),

        App.localPut(investor, LKEY_CONTRIB, App.localGet(investor, LKEY_CONTRIB) + Gtxn[1].amount()),
        App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) + Gtxn[1].amount()),
        Approve()
    )

    # ---------- helpers (constant-index batching) ----------
    contrib_amt = ScratchVar(TealType.uint64)
    tokens_due  = ScratchVar(TealType.uint64)

    def success_payout_one(acct_expr: Expr):
        """On success: send ASA proportional to contribution; zero local contrib; decrement global raised."""
        return Seq(
            If(App.optedIn(acct_expr, app_id)).Then(Seq(
                contrib_amt.store(App.localGet(acct_expr, LKEY_CONTRIB)),
                If(contrib_amt.load() > Int(0)).Then(Seq(
                    tokens_due.store((contrib_amt.load() * rate) / Int(1_000_000)),
                    If(tokens_due.load() > Int(0)).Then(Seq(
                        InnerTxnBuilder.Begin(),
                        InnerTxnBuilder.SetFields({
                            TxnField.type_enum:      TxnType.AssetTransfer,
                            TxnField.xfer_asset:     asa_id,
                            TxnField.asset_receiver: acct_expr,
                            TxnField.asset_amount:   tokens_due.load(),
                        }),
                        InnerTxnBuilder.Submit(),
                    )),
                    App.localPut(acct_expr, LKEY_CONTRIB, Int(0)),
                    App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) - contrib_amt.load()),
                ))
            ))
        )

    def refund_one(acct_expr: Expr):
        """On failure: refund ALGO equal to contribution; zero local contrib; decrement global raised."""
        return Seq(
            If(App.optedIn(acct_expr, app_id)).Then(Seq(
                contrib_amt.store(App.localGet(acct_expr, LKEY_CONTRIB)),
                If(contrib_amt.load() > Int(0)).Then(Seq(
                    InnerTxnBuilder.Begin(),
                    InnerTxnBuilder.SetFields({
                        TxnField.type_enum: TxnType.Payment,
                        TxnField.receiver:  acct_expr,
                        TxnField.amount:    contrib_amt.load(),
                    }),
                    InnerTxnBuilder.Submit(),
                    App.localPut(acct_expr, LKEY_CONTRIB, Int(0)),
                    App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) - contrib_amt.load()),
                ))
            ))
        )

    # Unrolled batch: sender + up to 4 extra accounts (expandable to 8 if you like)
    def success_payout_sender_and_accounts():
        return Seq(
            success_payout_one(Txn.sender()),
            If(Txn.accounts.length() >= Int(1)).Then(success_payout_one(Txn.accounts[0])),
            If(Txn.accounts.length() >= Int(2)).Then(success_payout_one(Txn.accounts[1])),
            If(Txn.accounts.length() >= Int(3)).Then(success_payout_one(Txn.accounts[2])),
            If(Txn.accounts.length() >= Int(4)).Then(success_payout_one(Txn.accounts[3])),
        )

    def refund_sender_and_accounts():
        return Seq(
            refund_one(Txn.sender()),
            If(Txn.accounts.length() >= Int(1)).Then(refund_one(Txn.accounts[0])),
            If(Txn.accounts.length() >= Int(2)).Then(refund_one(Txn.accounts[1])),
            If(Txn.accounts.length() >= Int(3)).Then(refund_one(Txn.accounts[2])),
            If(Txn.accounts.length() >= Int(4)).Then(refund_one(Txn.accounts[3])),
        )

    # ---------- finalize success (NO termination / NO admin fee / NO closes) ----------
    # After success payouts, transfer the AVAILABLE app ALGO balance to creator:
    # available = Balance(app) - MinBalance(app). We must leave min-balance in place.
    finalize = Seq(
        Assert(App.globalGet(KEY_RAISED) >= goal),
        Assert(before_deadline),

        success_payout_sender_and_accounts(),

        (available := ScratchVar(TealType.uint64)).store(Balance(app_addr) - MinBalance(app_addr)),
        If(available.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  creator,
                TxnField.amount:    available.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),
        Approve()
    )

    # refund failure (kept as-is; still returns ALGO to contributors and may close if you want to keep it)
    refund = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_RAISED) < goal),

        refund_sender_and_accounts(),

        # Optional: you can keep or remove the close-out-on-refund logic; leaving it here is fine.
        If(App.globalGet(KEY_RAISED) == Int(0)).Then(Seq(
            # close ASA to creator (so app can reclaim min-balance if later closed by DeleteApplication)
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum:      TxnType.AssetTransfer,
                TxnField.xfer_asset:     asa_id,
                TxnField.asset_receiver: creator,
                TxnField.asset_amount:   Int(0),
                TxnField.asset_close_to: creator,
            }),
            InnerTxnBuilder.Submit(),

            # split deposit: half to admin; close remainder to creator
            (half := ScratchVar(TealType.uint64)).store(deposit / Int(2)),
            If(half.load() > Int(0)).Then(Seq(
                InnerTxnBuilder.Begin(),
                InnerTxnBuilder.SetFields({
                    TxnField.type_enum: TxnType.Payment,
                    TxnField.receiver:  admin,
                    TxnField.amount:    half.load(),
                }),
                InnerTxnBuilder.Submit(),
            )),
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum:          TxnType.Payment,
                TxnField.receiver:           creator,
                TxnField.amount:             Int(0),
                TxnField.close_remainder_to: creator,
            }),
            InnerTxnBuilder.Submit(),
        )),
        Approve()
    )

    on_delete   = Seq(Assert(is_creator), Approve())
    on_update   = Seq(Reject())
    on_closeout = Seq(Approve())
    on_optin    = Seq(App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)), Approve())

    program = Cond(
        [Txn.application_id() == Int(0), on_create],
        [Txn.on_completion() == OnComplete.UpdateApplication, on_update],
        [Txn.on_completion() == OnComplete.DeleteApplication, on_delete],
        [Txn.on_completion() == OnComplete.CloseOut, on_closeout],
        [Txn.on_completion() == OnComplete.OptIn, on_optin],
        [Txn.on_completion() == OnComplete.NoOp, Cond(
            [Txn.application_args[0] == Bytes("setup"),      setup],
            [Txn.application_args[0] == Bytes("contribute"), contribute],
            [Txn.application_args[0] == Bytes("finalize"),   finalize],
            [Txn.application_args[0] == Bytes("refund"),     refund],
        )]
    )
    return program


def clear_program():
    return Approve()
