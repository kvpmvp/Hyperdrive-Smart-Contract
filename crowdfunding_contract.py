from pyteal import *

# --------- Global keys ---------
KEY_GOAL     = Bytes("goal")       # microAlgos
KEY_RATE     = Bytes("rate")       # tokens per 1e6 microAlgos
KEY_DEADLINE = Bytes("deadline")   # round
KEY_ASA      = Bytes("asa_id")
KEY_RAISED   = Bytes("raised")     # total contributed, does NOT decrement on claim
KEY_DEPOSIT  = Bytes("deposit")    # developer's 2% deposit (microAlgos)
KEY_CREATOR  = Bytes("creator")
KEY_ADMIN    = Bytes("admin")
KEY_FUNDED   = Bytes("funded")     # 0 or 1 latch

# --------- Local keys ----------
LKEY_CONTRIB = Bytes("contrib")    # per‑investor contributed microAlgos

def approval_program():
    # ----- Create -----
    admin_arg    = Txn.application_args[0]
    goal_arg     = Btoi(Txn.application_args[1])
    rate_arg     = Btoi(Txn.application_args[2])
    deadline_arg = Btoi(Txn.application_args[3])

    on_create = Seq(
        Assert(Txn.application_args.length() == Int(4)),
        # Basic input sanity
        Assert(Len(admin_arg) == Int(32)),
        Assert(goal_arg > Int(0)),
        Assert(rate_arg > Int(0)),
        Assert(deadline_arg > Global.round()),

        App.globalPut(KEY_CREATOR, Txn.sender()),
        App.globalPut(KEY_ADMIN,   admin_arg),
        App.globalPut(KEY_GOAL,    goal_arg),
        App.globalPut(KEY_RATE,    rate_arg),
        App.globalPut(KEY_DEADLINE,deadline_arg),

        App.globalPut(KEY_RAISED,  Int(0)),
        App.globalPut(KEY_ASA,     Int(0)),
        App.globalPut(KEY_DEPOSIT, Int(0)),
        App.globalPut(KEY_FUNDED,  Int(0)),
        Approve(),
    )

    # ----- Common symbols -----
    app_addr = Global.current_application_address()
    goal     = App.globalGet(KEY_GOAL)
    rate     = App.globalGet(KEY_RATE)
    deadline = App.globalGet(KEY_DEADLINE)
    asa_id   = App.globalGet(KEY_ASA)
    deposit  = App.globalGet(KEY_DEPOSIT)
    creator  = App.globalGet(KEY_CREATOR)
    admin    = App.globalGet(KEY_ADMIN)
    funded   = App.globalGet(KEY_FUNDED)

    is_creator      = Txn.sender() == creator
    before_deadline = Global.round() <= deadline
    after_deadline  = Global.round() >  deadline

    # ----- setup (one‑time) -----
    # Expected group layout:
    #   [0] Payment  (creator -> app)   amount == 2% of goal (deposit)
    #   [1] AppCall  ("setup", asa_id)
    #   [2] Asset Xfer (creator -> app) amount >= goal*rate/1e6  (seed ASA)
    setup_asa = Btoi(Txn.application_args[1])
    expected_deposit = (goal * Int(2)) / Int(100)

    # ASA safety parameters (freeze/clawback must be disabled; not default frozen)
    asa_df   = AssetParam.defaultFrozen(setup_asa)
    asa_frz  = AssetParam.freeze(setup_asa)
    asa_claw = AssetParam.clawback(setup_asa)

    setup = Seq(
        Assert(is_creator),
        Assert(Txn.application_args.length() == Int(2)),
        # One‑time, pre‑funding lock
        Assert(asa_id == Int(0)),
        Assert(deposit == Int(0)),
        Assert(App.globalGet(KEY_RAISED) == Int(0)),
        Assert(funded == Int(0)),

        # Exact group and ordering
        Assert(Global.group_size() == Int(3)),

        # --- deposit payment guards ---
        Assert(Gtxn[0].type_enum() == TxnType.Payment),
        Assert(Gtxn[0].sender()    == Txn.sender()),
        Assert(Gtxn[0].receiver()  == app_addr),
        Assert(Gtxn[0].amount()    == expected_deposit),
        Assert(Gtxn[0].rekey_to()  == Global.zero_address()),
        Assert(Gtxn[0].close_remainder_to() == Global.zero_address()),
        App.globalPut(KEY_DEPOSIT, Gtxn[0].amount()),

        # Set ASA id
        App.globalPut(KEY_ASA, setup_asa),

        # ASA policy checks
        asa_df,  asa_frz,  asa_claw,
        Assert(asa_df.value() == Int(0)),                           # not default frozen
        Assert(asa_frz.value() == Global.zero_address()),           # no freeze addr
        Assert(asa_claw.value() == Global.zero_address()),          # no clawback

        # App opt‑in to ASA (inner)
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields({
            TxnField.type_enum:      TxnType.AssetTransfer,
            TxnField.xfer_asset:     setup_asa,
            TxnField.asset_receiver: app_addr,
            TxnField.asset_amount:   Int(0),
        }),
        InnerTxnBuilder.Submit(),

        # --- seed ASA transfer guards ---
        Assert(Gtxn[2].type_enum()      == TxnType.AssetTransfer),
        Assert(Gtxn[2].sender()         == Txn.sender()),
        Assert(Gtxn[2].asset_receiver() == app_addr),
        Assert(Gtxn[2].xfer_asset()     == setup_asa),
        Assert(Gtxn[2].asset_close_to() == Global.zero_address()),
        Assert(Gtxn[2].rekey_to()       == Global.zero_address()),
        # Need at least enough ASA to satisfy full success:
        Assert(Gtxn[2].asset_amount()   >= WideRatio([goal, rate],[Int(1_000_000)])),
        Approve(),
    )

    # ----- contribute -----
    # Expected group: [0] AppCall("contribute"), [1] Payment(investor->app)
    investor = Txn.sender()
    contribute = Seq(
        # Investors may contribute any positive amount; no 2% check here.
        Assert(funded == Int(0)),                       # freeze new funding once latched
        Assert(asa_id != Int(0)),                       # setup must have happened
        Assert(before_deadline),
        Assert(Global.group_size() == Int(2)),

        Assert(Gtxn[1].type_enum() == TxnType.Payment),
        Assert(Gtxn[1].sender()    == investor),
        Assert(Gtxn[1].receiver()  == app_addr),
        Assert(Gtxn[1].amount()    >  Int(0)),
        Assert(Gtxn[1].rekey_to()  == Global.zero_address()),
        Assert(Gtxn[1].close_remainder_to() == Global.zero_address()),

        # Prevent oversubscription
        Assert(App.globalGet(KEY_RAISED) + Gtxn[1].amount() <= goal),

        App.localPut(investor, LKEY_CONTRIB,
                     App.localGet(investor, LKEY_CONTRIB) + Gtxn[1].amount()),
        App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) + Gtxn[1].amount()),
        If(App.globalGet(KEY_RAISED) == goal).Then(App.globalPut(KEY_FUNDED, Int(1))),
        Approve(),
    )

    # ----- claim (investor gets ASA) -----
    contrib_amt = ScratchVar(TealType.uint64)
    tokens_due  = ScratchVar(TealType.uint64)

    claim = Seq(
        Assert(funded == Int(1)),
        Assert(Txn.assets.length() >= Int(1)),
        Assert(Txn.assets[0] == asa_id),

        contrib_amt.store(App.localGet(Txn.sender(), LKEY_CONTRIB)),
        Assert(contrib_amt.load() > Int(0)),
        tokens_due.store(WideRatio([contrib_amt.load(), rate],[Int(1_000_000)])),

        If(tokens_due.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum:      TxnType.AssetTransfer,
                TxnField.xfer_asset:     asa_id,
                TxnField.asset_receiver: Txn.sender(),
                TxnField.asset_amount:   tokens_due.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),
        # Zero local contrib; DO NOT decrement global raised (kept as total)
        App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)),
        Approve(),
    )

    # ----- withdraw (creator payout, admin 2% of total raised) -----
    unlocked   = ScratchVar(TealType.uint64)
    admin_fee  = ScratchVar(TealType.uint64)
    to_creator = ScratchVar(TealType.uint64)

    withdraw = Seq(
        Assert(is_creator),
        Assert(funded == Int(1)),  # success only

        unlocked.store(Balance(app_addr) - MinBalance(app_addr)),
        # Platform fee = 2% of total raised (latched at goal)
        admin_fee.store((App.globalGet(KEY_RAISED) * Int(2)) / Int(100)),
        # Cap fee to what's available
        admin_fee.store(If(admin_fee.load() <= unlocked.load(), admin_fee.load(), unlocked.load())),
        to_creator.store(unlocked.load() - admin_fee.load()),

        If(admin_fee.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  admin,
                TxnField.amount:    admin_fee.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),

        If(to_creator.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  creator,
                TxnField.amount:    to_creator.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),
        Approve(),
    )

    # ----- refund (investor gets ALGO back on failure) -----
    refund = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_FUNDED) == Int(0)),

        contrib_amt.store(App.localGet(Txn.sender(), LKEY_CONTRIB)),
        Assert(contrib_amt.load() > Int(0)),

        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields({
            TxnField.type_enum: TxnType.Payment,
            TxnField.receiver:  Txn.sender(),
            TxnField.amount:    contrib_amt.load(),
        }),
        InnerTxnBuilder.Submit(),

        App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)),
        App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) - contrib_amt.load()),
        Approve(),
    )

    # ----- reclaim (split developer deposit 50/50 on failure) -----
    half        = ScratchVar(TealType.uint64)
    pay_admin   = ScratchVar(TealType.uint64)
    pay_creator = ScratchVar(TealType.uint64)

    reclaim = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_FUNDED) == Int(0)),
        Assert(is_creator),

        half.store(deposit / Int(2)),
        unlocked.store(Balance(app_addr) - MinBalance(app_addr)),

        pay_admin.store(If(unlocked.load() < half.load(), unlocked.load(), half.load())),
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
        Approve(),
    )

    # ----- sweep ASA back to creator after failed campaign fully refunded -----
    asa_bal = AssetHolding.balance(app_addr, asa_id)

    sweep_asa_failed = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_FUNDED) == Int(0)),
        Assert(is_creator),
        Assert(App.globalGet(KEY_RAISED) == Int(0)),     # all refunds complete
        Assert(asa_id != Int(0)),

        asa_bal,
        If(And(asa_bal.hasValue(), asa_bal.value() > Int(0))).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum:      TxnType.AssetTransfer,
                TxnField.xfer_asset:     asa_id,
                TxnField.asset_receiver: creator,
                TxnField.asset_amount:   asa_bal.value(),
            }),
            InnerTxnBuilder.Submit(),
        )),
        Approve(),
    )

    # ----- lifecycle handlers -----
    on_update   = Seq(Reject())
    on_delete   = Seq(Reject())  # keep app open; explicit sweep/withdraw flows instead

    # Prevent users from losing refunds/claims by clearing/closeout with nonzero contrib
    on_closeout = Seq(
        Assert(App.localGet(Txn.sender(), LKEY_CONTRIB) == Int(0)),
        Approve()
    )

    on_optin    = Seq(App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)), Approve())

    program = Cond(
        [Txn.application_id() == Int(0), on_create],
        [Txn.on_completion() == OnComplete.UpdateApplication, on_update],
        [Txn.on_completion() == OnComplete.DeleteApplication, on_delete],
        [Txn.on_completion() == OnComplete.CloseOut, on_closeout],
        [Txn.on_completion() == OnComplete.OptIn, on_optin],
        [Txn.on_completion() == OnComplete.NoOp, Cond(
            [Txn.application_args[0] == Bytes("setup"),           setup],
            [Txn.application_args[0] == Bytes("contribute"),      contribute],
            [Txn.application_args[0] == Bytes("claim"),           claim],
            [Txn.application_args[0] == Bytes("withdraw"),        withdraw],
            [Txn.application_args[0] == Bytes("refund"),          refund],
            [Txn.application_args[0] == Bytes("reclaim"),         reclaim],
            [Txn.application_args[0] == Bytes("sweep_asa_failed"),sweep_asa_failed],
        )]
    )
    return program

def clear_program():
    # Disallow ClearState if the caller still has a nonzero contribution.
    return Seq(
        Assert(App.localGet(Txn.sender(), LKEY_CONTRIB) == Int(0)),
        Approve()
    )
