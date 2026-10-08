"""Native controls for Charm. Only explicit apply changes the Hermes checkout."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def setup(parser, ctx=None):
    parser.add_argument('action', choices=['check', 'list', 'enable', 'disable', 'apply', 'drift'], nargs='?',
                        default='check')
    parser.add_argument('customization', nargs='?')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Preflight only, including with apply')
    mode.add_argument('--rollback', type=Path, help='Restore a verified installation receipt')
    parser.add_argument('--ref', default='origin/main',
                        help='drift: upstream revision to test the bundle against (fetched first)')
    parser.add_argument('--tests', action='store_true', help='drift: also run the tests the bundle carries')
    parser.set_defaults(func=run, charm_ctx=ctx)


def catalog(ctx=None):
    data = json.loads(Path(__file__).resolve().with_name('customizations.json').read_text())
    for row in data['customizations']:
        enabled = ctx.get_config(row['id'], row['default_enabled']) if ctx else row['default_enabled']
        if type(enabled) is not bool:
            raise ValueError(f"{row['id']} must be a boolean, not {enabled!r}")
        row['desired_enabled'] = enabled
    return data


def drift(args):
    """Rehearse the bundle on the revision `hermes update` would install. Never writes artifacts."""
    import os
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    agent = Path(os.environ.get('LOCAL_PATCHES_AGENT_DIR', str(home / 'hermes-agent')))
    patches = os.environ.get('LOCAL_PATCHES_DIR', str(Path(__file__).resolve().with_name('local-patches')))
    if args.ref.startswith('origin/'):
        # Updates remote-tracking refs only; the checkout, index and HEAD stay put.
        fetch = subprocess.run(['git', '-C', str(agent), 'fetch', '-q', 'origin', args.ref.split('/', 1)[1]],
                               stdin=subprocess.DEVNULL, timeout=300)
        if fetch.returncode:
            raise ValueError(f'Could not fetch {args.ref}')
    command = [sys.executable, str(Path(__file__).resolve().with_name('scripts') / 'patch_series.py'),
               'rebase', '--agent', str(agent), '--patches', patches, '--ref', args.ref]
    if args.tests:
        command.append('--tests')
    return subprocess.run(command, stdin=subprocess.DEVNULL, timeout=2400).returncode


def run(args):
    try:
        if (args.ref != 'origin/main' or args.tests) and args.action != 'drift':
            raise ValueError('--ref and --tests only apply to drift')
        if (args.check or args.rollback) and args.action not in ('check', 'apply'):
            raise ValueError('Operation flags require check/apply')
        if args.rollback and args.action != 'apply':
            raise ValueError('Rollback requires explicit apply')
        data = catalog(args.charm_ctx)
        if args.action in ('enable', 'disable'):
            known = {row['id'] for row in data['customizations']}
            if args.customization not in known:
                raise ValueError(f'Unknown customization: {args.customization!r}')
            if args.charm_ctx is None:
                raise ValueError('Use native hermes charm to change profile settings')
            enabled = args.action == 'enable'
            args.charm_ctx.set_config(args.customization, enabled)
            if args.charm_ctx.get_config(args.customization) is not enabled:
                raise ValueError('Configuration read-back did not match the requested toggle')
            print(json.dumps({'id': args.customization, 'desired_enabled': enabled,
                              'activation': 'Run hermes charm check, then explicit apply and a safe backend restart.'}))
            raise SystemExit(0)
        if args.customization is not None:
            raise ValueError('A customization name is only accepted by enable/disable')
        if args.action == 'drift':
            raise SystemExit(drift(args))
        if args.action == 'list':
            print(json.dumps(data, indent=2))
            raise SystemExit(0)
    except (OSError, ValueError, KeyError) as exc:
        print(f'charm: REFUSED: {exc}', file=sys.stderr)
        raise SystemExit(1)
    engine = Path(__file__).resolve().with_name('local_patches.py')
    disabled = [row['id'] for row in data['customizations'] if not row['desired_enabled']]
    command = [sys.executable, str(engine)]
    if args.rollback:
        command.extend(['--rollback', str(args.rollback)])
    else:
        if args.action == 'check' or args.check:
            command.append('--check')
        for name in disabled:
            command.extend(['--disable', name])
    result = subprocess.run(command,
                            stdin=subprocess.DEVNULL, timeout=120)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    setup(parser)
    run(parser.parse_args())
