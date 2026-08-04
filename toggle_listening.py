#!/usr/bin/env python3
"""Command-line switch for the AgiBot X2's audience listening.

Examples:
    python toggle_listening.py off      # robot stops capturing audio
    python toggle_listening.py on       # robot resumes listening
    python toggle_listening.py toggle   # flip the current state
    python toggle_listening.py status   # show the current state
"""

import argparse
import logging
import sys

from agibot_x2 import ListeningController


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=["on", "off", "toggle", "status"],
        help="on = start listening, off = stop listening, "
        "toggle = flip state, status = print current state",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    controller = ListeningController()

    if args.command == "on":
        controller.start_listening()
    elif args.command == "off":
        controller.stop_listening()
    elif args.command == "toggle":
        controller.toggle()

    state = "LISTENING" if controller.is_listening else "NOT LISTENING (muted)"
    print(f"AgiBot X2 microphone: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
