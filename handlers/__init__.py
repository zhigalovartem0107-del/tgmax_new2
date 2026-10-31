from maxapi import Router

from handlers import generation, payments, start


def get_routers() -> list[Router]:
    # Order matters: the first router whose filters match handles the update.
    return [start.router, payments.router, generation.router]
