
from pyteal import *

def clear():
    return Approve()

if __name__ == "__main__":
    print(compileTeal(clear(), Mode.Application, version=8))
