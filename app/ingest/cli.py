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
        from app.ingest.filings import ingest_filings

        ingest_filings()
        return
    if args.command == "prices":
        from app.ingest.prices import ingest_prices

        ingest_prices()
        return

    from app.ingest.filings import ingest_filings
    from app.ingest.prices import ingest_prices
    from app.ingest.xbrl import ingest_xbrl

    ingest_xbrl()
    ingest_prices()
    ingest_filings()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
