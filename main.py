"""Single command for the BTC five-minute prediction service."""

import argparse
from pathlib import Path

from bot.runtime import serve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-only", action="store_true",
                        help="Serve saved data without starting live collection")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    serve(args.data, args.host, args.port, args.api_only)


if __name__ == "__main__":
    main()
