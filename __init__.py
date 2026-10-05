"""Charm registers read-only maintenance commands through the native plugin API."""
from .charm import run, setup


def register(ctx):
    ctx.register_cli_command(name='charm', help='Check Charm and its pinned core patch bundle',
                             setup_fn=setup, handler_fn=run)
