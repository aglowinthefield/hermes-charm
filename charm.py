"""Read-only native CLI for Charm. Patch installation is an explicit companion script."""
import argparse
from pathlib import Path
import subprocess
import sys


def setup(parser):
    parser.add_argument('action', choices=['check'], nargs='?', default='check')
    parser.set_defaults(func=run)


def run(args):
    del args
    engine = Path(__file__).resolve().with_name('local_patches.py')
    result = subprocess.run([sys.executable, str(engine), '--check'],
                            stdin=subprocess.DEVNULL, timeout=120)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    setup(parser)
    run(parser.parse_args())
