from pyteal import *

# --------- Global keys ---------
KEY_GOAL     = Bytes("goal")
KEY_RATE     = Bytes("rate")
KEY_DEADLINE = Bytes("deadline")
KEY_ASA      = Bytes("asa_id")
KEY_RAISED   = Bytes("raised")
KEY_DEPOSIT  = Bytes("deposit")
KEY_CREATOR  = Bytes("creator")
KEY_ADMIN    = Bytes("admin")

# --------- Local keys ----------
LKEY_CONTRIB = Bytes("contrib")

# How many foreign accounts (investors) to process per call (unrolled)
MAX_ACCOUNTS = 8

def approval_program():
    # ----- On create -----
    on_create = Seq(
        Assert(Txn.application_args.length() == Int(4)),
        App.globalPut(KEY_CREATOR, Txn.sender()),
        App.globalPut(KEY_ADMIN,   Txn.application_args[0]),  # admin address (bytes)
        App.globalPut(KEY_GOAL,    Btoi(Txn.application_args[1])),
        App.globalPut(KEY_RATE,    Btoi(Txn.application_args[2])),
        App.globalPut(KEY_DEADLINE,Btoi(Txn.application_args[3])),
        App.globalPut(KEY_RAISED,  Int(0)),
        App.globalPut(KEY_ASA,     Int(0)),
        App.globalPut(KEY_DEPOSIT, Int(0)),
        Approve(),
    )

    # ----- Common symbols -----
    app_id   = Global.current_application_id()
    app_addr = Global.current_application_address()
    goal     = App.globalGet(KEY_GOAL)
    rate     = App.globalGet(KEY_RATE)
    deadline = App.globalGet(KEY_DEADLINE)
    asa_id   = App.globalGet(KEY_ASA)
    deposit  = App.globalGet(KEY_DEPOSIT)
    creator  = App.globalGet(KEY_CREATOR)
    admin    = App.globalGet(KEY_ADMIN)

    is_creator      = Txn.sender() == creator
    before_deadline = Global.round() <= deadline
    after_deadline  = Global.round() >  deadline

    # ----- setup -----
    # Expect group: [0]=Payment(deposit->app), [1]=AppCall("setup", assets=[asa]), [2]=ASA Transfer(seed->app)
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

        # inner opt-in (now funded for min balance)
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields({
            TxnField.type_enum:      TxnType.AssetTransfer,
            TxnField.xfer_asset:     Txn.assets[0],
            TxnField.asset_receiver: app_addr,
            TxnField.asset_amount:   Int(0),
        }),
        InnerTxnBuilder.Submit(),

        # seed tokens (at least enough to satisfy full success: goal * rate / 1e6)
        Assert(Gtxn[2].type_enum()      == TxnType.AssetTransfer),
        Assert(Gtxn[2].sender()         == Txn.sender()),
        Assert(Gtxn[2].asset_receiver() == app_addr),
        Assert(Gtxn[2].xfer_asset()     == Txn.assets[0]),
        Assert(Gtxn[2].asset_amount()   >= (goal * rate) / Int(1_000_000)),
        Approve()
    )

    # ----- contribute -----
    # Expect per-investor group: [AppCall("contribute"), Payment(investor->app)]
    investor = Txn.sender()
    contribute = Seq(
        Assert(before_deadline),
        Assert(Global.group_size() >= Int(2)),
        Assert(Gtxn[1].type_enum() == TxnType.Payment),
        Assert(Gtxn[1].sender()    == investor),
        Assert(Gtxn[1].receiver()  == app_addr),
        Assert(Gtxn[1].amount()    >  Int(0)),
        # Cap: prevent oversubscription
        Assert(App.globalGet(KEY_RAISED) + Gtxn[1].amount() <= goal),

        App.localPut(investor, LKEY_CONTRIB,
                     App.localGet(investor, LKEY_CONTRIB) + Gtxn[1].amount()),
        App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) + Gtxn[1].amount()),
        Approve()
    )

    # ----- helpers -----
    acct        = ScratchVar(TealType.bytes)
    contrib_amt = ScratchVar(TealType.uint64)
    tokens_due  = ScratchVar(TealType.uint64)

    def payout_one(acct_expr: Expr):
        """Send ASA to an investor for their contribution and zero it out."""
        return Seq(
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
        )

    def refund_one(acct_expr: Expr):
        """Refund ALGO contribution to an investor and zero it out."""
        return Seq(
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
        )

    # Unrolled handler for foreign account at constant idx
    def handle_account_at(idx: int, body_fn):
        return If(Txn.accounts.length() > Int(idx)).Then(
            Seq(
                acct.store(Txn.accounts[idx]),
                If(App.optedIn(acct.load(), app_id)).Then(body_fn(acct.load()))
            )
        )

    def unrolled_for_accounts(body_fn):
        seqs = []
        for k in range(MAX_ACCOUNTS):
            seqs.append(handle_account_at(k, body_fn))
        return Seq(*seqs) if len(seqs) > 0 else Approve()

    # ----- withdraw (success path; does NOT close app) -----
    # - only creator
    # - require raised >= goal (funded)
    # - process up to MAX_ACCOUNTS investors from Txn.accounts
    # - pay admin 2% of UNLOCKED (Balance - MinBalance), dev gets rest
    # - leave account open
    unlocked   = ScratchVar(TealType.uint64)
    admin_fee  = ScratchVar(TealType.uint64)
    to_creator = ScratchVar(TealType.uint64)

    withdraw = Seq(
        Assert(is_creator),
        Assert(App.globalGet(KEY_RAISED) >= goal),

        # distribute ASA to provided investors (constant-indexed unroll)
        unrolled_for_accounts(payout_one),

        # compute unlocked = Balance - MinBalance (do NOT close account)
        unlocked.store(Balance(app_addr) - MinBalance(app_addr)),
        admin_fee.store((unlocked.load() * Int(2)) / Int(100)),
        to_creator.store(unlocked.load() - admin_fee.load()),

        # pay admin fee
        If(admin_fee.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  admin,
                TxnField.amount:    admin_fee.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),

        # pay remainder to creator
        If(to_creator.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  creator,
                TxnField.amount:    to_creator.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),

        Approve()
    )

    # ----- refund (failure path; does NOT close app) -----
    # After deadline and not funded: refund investors provided in Txn.accounts.
    # Then split deposit 50/50 between admin and creator, but capped to the unlocked balance.
    half        = ScratchVar(TealType.uint64)
    pay_admin   = ScratchVar(TealType.uint64)
    pay_creator = ScratchVar(TealType.uint64)

    refund = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_RAISED) < goal),

        # refund each provided investor
        unrolled_for_accounts(refund_one),

        # split deposit within what's unlocked
        unlocked.store(Balance(app_addr) - MinBalance(app_addr)),
        half.store(deposit / Int(2)),

        # admin gets min(half, unlocked)
        pay_admin.store(If(unlocked.load() < half.load(), unlocked.load(), half.load())),
        # creator gets min(half, unlocked - pay_admin)
        pay_creator.store(
            If((unlocked.load() - pay_admin.load()) < half.load(),
               unlocked.load() - pay_admin.load(),
               half.load())
        ),

        If(pay_admin.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  admin,
                TxnField.amount:    pay_admin.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),
        If(pay_creator.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  creator,
                TxnField.amount:    pay_creator.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),

        Approve()
    )

    # ----- other handlers -----
    on_update   = Seq(Reject())
    on_delete   = Seq(Reject())  # keep app open; avoid delete/close complexities
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
            [Txn.application_args[0] == Bytes("withdraw"),   withdraw],
            [Txn.application_args[0] == Bytes("refund"),     refund],
        )]
    )
    return program

def clear_program():
    return Approve()
