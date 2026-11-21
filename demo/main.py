"""demo-site: a tiny CLI the hackathon demo agent can safely edit.

Deliberately missing: --version flag (beat 1 asks the agent to add it).
"""

import argparse
import sys

APP_NAME = "demo-site"
TAGLINE = "a very small website generator"


def greet(name: str) -> str:
    return f"hello, {name} — {TAGLINE}"


def word_count(text: str) -> int:
    return len(text.split())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description=TAGLINE)
    parser.add_argument("--version", action="version", version=APP_NAME)
    parser.add_argument("name", nargs="?", default="world")
    args = parser.parse_args(argv)
    print(greet(args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
