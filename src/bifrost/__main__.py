"""Command-line entry point for ``python -m bifrost``."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bifrost",
        description="Bifrost WebRTC private HTTP access tool",
        add_help=False,
    )
    parser.add_argument(
        "component",
        choices=("server", "client", "curl", "hash-password"),
        help="component to run; use its module or installed console script for options",
    )
    if len(sys.argv) == 1 or sys.argv[1] in {"-h", "--help"}:
        print(parser.format_help())
        return
    args, rest = parser.parse_known_args()
    if args.component == "server":
        from .server import main as run
    elif args.component == "client":
        from .client import cli as run
    elif args.component == "curl":
        from .curl import cli as run
    else:
        from .room_auth import main as run
    # The component parsers currently read sys.argv directly.
    sys.argv[1:] = rest
    run()


if __name__ == "__main__":
    main()
