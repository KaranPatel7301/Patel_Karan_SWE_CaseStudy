"""Ingestion commands: python -m app.ingest.cli [xbrl|filings|prices|all]."""

import argparse
import sys

from app.main import configure_logging


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m app.ingest.cli")
    parser.add_argument("command", choices=["xbrl", "filings", "prices", "all"])
    args = parser.parse_args(argv)

    if args.command == "xbrl":
        from app.ingest.xbrl import ingest_xbrl

        ingest_xbrl()
        return
    if args.command == "filings":
        raise SystemExit("filings ingestion is not implemented yet")
    if args.command == "prices":
        raise SystemExit("prices ingestion is not implemented yet")

    from app.ingest.xbrl import ingest_xbrl

    ingest_xbrl()
    raise SystemExit("filings and prices ingestion are not implemented yet")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
