"""Charm registers controls through the native plugin API without modifying core."""
from .charm import run, setup
from functools import partial


def register(ctx):
    ctx.register_cli_command(name='charm', help='Inspect, configure and explicitly apply Charm customizations',
                             setup_fn=partial(setup, ctx=ctx), handler_fn=run)
