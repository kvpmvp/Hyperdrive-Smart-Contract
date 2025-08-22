
import os
from pyteal import *
from contracts.approval import approval
from contracts.clear import clear

BUILD_DIR = os.path.join(os.path.dirname(__file__), "..", "build")
os.makedirs(BUILD_DIR, exist_ok=True)

def main():
    approval_teal = compileTeal(approval(), Mode.Application, version=8)
    clear_teal = compileTeal(clear(), Mode.Application, version=8)

    with open(os.path.join(BUILD_DIR, "approval.teal"), "w") as f:
        f.write(approval_teal)
    with open(os.path.join(BUILD_DIR, "clear.teal"), "w") as f:
        f.write(clear_teal)
    print("Wrote build/approval.teal and build/clear.teal")

if __name__ == "__main__":
    main()
