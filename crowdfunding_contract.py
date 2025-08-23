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
KEY_FUNDED   = Bytes("funded")  # 0 or 1

# --------- Local keys ----------
LKEY_CONTRIB = Bytes("contrib")

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

    # ----- setup -----
    # Expect group: [0]=Payment(deposit->app), [1]=AppCall("setup", app_args=["setup", asa_id]), [2]=ASA Transfer(seed->app)
    setup_asa = Btoi(Txn.application_args[1])  # ASA id passed explicitly as app arg
    expected_deposit = (goal * Int(2)) / Int(100)
    setup = Seq(
        Assert(is_creator),
        Assert(Txn.application_args.length() == Int(2)),
        App.globalPut(KEY_ASA, setup_asa),
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
            TxnField.xfer_asset:     setup_asa,
            TxnField.asset_receiver: app_addr,
            TxnField.asset_amount:   Int(0),
        }),
        InnerTxnBuilder.Submit(),

        # seed tokens (at least enough to satisfy full success: goal * rate / 1e6)
        Assert(Gtxn[2].type_enum()      == TxnType.AssetTransfer),
        Assert(Gtxn[2].sender()         == Txn.sender()),
        Assert(Gtxn[2].asset_receiver() == app_addr),
        Assert(Gtxn[2].xfer_asset()     == setup_asa),
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
        # If we just reached the goal, latch funded
        If(App.globalGet(KEY_RAISED) == goal).Then(App.globalPut(KEY_FUNDED, Int(1))),
        Approve()
    )

    # ----- claim (investor self-serve ASA) -----
    contrib_amt = ScratchVar(TealType.uint64)
    tokens_due  = ScratchVar(TealType.uint64)

    claim = Seq(
        Assert(funded == Int(1)),             # project is funded (latched)
        Assert(Txn.assets.length() >= Int(1)),
        Assert(Txn.assets[0] == asa_id),
        contrib_amt.store(App.localGet(Txn.sender(), LKEY_CONTRIB)),
        Assert(contrib_amt.load() > Int(0)),
        tokens_due.store((contrib_amt.load() * rate) / Int(1_000_000)),
        # send ASA to the caller
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
        # zero out their contrib and decrement raised
        App.localPut(Txn.sender(), LKEY_CONTRIB, Int(0)),
        App.globalPut(KEY_RAISED, App.globalGet(KEY_RAISED) - contrib_amt.load()),
        Approve()
    )

    # ----- withdraw (creator ALGO payout, admin 2%) -----
    unlocked   = ScratchVar(TealType.uint64)
    admin_fee  = ScratchVar(TealType.uint64)
    to_creator = ScratchVar(TealType.uint64)

    # Allow withdraw if: funded latched, OR (raised==goal), OR (raised==0 after all claims)
    can_withdraw = Or(
        App.globalGet(KEY_FUNDED) == Int(1),
        App.globalGet(KEY_RAISED) == App.globalGet(KEY_GOAL),
        App.globalGet(KEY_RAISED) == Int(0),
    )

    withdraw = Seq(
        Assert(is_creator),
        Assert(can_withdraw),

        # Admin must be provided in foreign accounts to satisfy AVM account availability
        Assert(Txn.accounts.length() >= Int(1)),
        # NOTE: We *don't* assert Txn.accounts[0] == admin; the receiver below comes from global state.

        # compute unlocked = Balance - MinBalance (leave account open)
        unlocked.store(Balance(app_addr) - MinBalance(app_addr)),
        admin_fee.store((unlocked.load() * Int(2)) / Int(100)),
        to_creator.store(unlocked.load() - admin_fee.load()),

        # pay admin (to the address stored in global state)
        If(admin_fee.load() > Int(0)).Then(Seq(
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.Payment,
                TxnField.receiver:  admin,
                TxnField.amount:    admin_fee.load(),
            }),
            InnerTxnBuilder.Submit(),
        )),

        # pay creator (sender)
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

    # ----- refund (investor self-serve ALGO) -----
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
        Approve()
    )

    # Optional: creator can reclaim deposit halves after failure (admin must be in accounts[0])
    half        = ScratchVar(TealType.uint64)
    pay_admin   = ScratchVar(TealType.uint64)
    pay_creator = ScratchVar(TealType.uint64)

    reclaim = Seq(
        Assert(after_deadline),
        Assert(App.globalGet(KEY_FUNDED) == Int(0)),
        Assert(is_creator),
        Assert(Txn.accounts.length() >= Int(1)),
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
        Approve()
    )

    # ----- other handlers -----
    on_update   = Seq(Reject())
    on_delete   = Seq(Reject())  # keep app open
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
            [Txn.application_args[0] == Bytes("claim"),      claim],
            [Txn.application_args[0] == Bytes("withdraw"),   withdraw],
            [Txn.application_args[0] == Bytes("refund"),     refund],
            [Txn.application_args[0] == Bytes("reclaim"),    reclaim],
        )]
    )
    return program

def clear_program():
    return Approve()
