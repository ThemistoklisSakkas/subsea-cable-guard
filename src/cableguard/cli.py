"""Command line: python -m cableguard <command>."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from cableguard.config import collector_config, get_secret, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cableguard")
    ap.add_argument("--config", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="run the real-time AIS collector")
    c.add_argument("--duration-s", type=float, default=None,
                   help="stop after N seconds (for testing); default: run forever")
    sub.add_parser("compact", help="merge finished days' part files into daily files")
    sub.add_parser("status", help="summarise collected data and the last heartbeat")
    for name, text in [("infra-fetch", "download cable/pipeline geometries for a region"),
                       ("phase1", "build infrastructure layer, buffers, density map")]:
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--region", required=True)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)

    if args.cmd in ("infra-fetch", "phase1"):
        import json

        from cableguard import phase1
        from cableguard.regions import get_region
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        region = get_region(cfg, args.region)
        if args.cmd == "infra-fetch":
            phase1.fetch(cfg, region)
        else:
            print(json.dumps(phase1.build(cfg, region), indent=2))
        return 0

    ccfg = collector_config(cfg)

    if args.cmd == "collect":
        from cableguard.collector.client import SingleInstance, run, setup_logging
        setup_logging(ccfg.log_dir)
        api_key = get_secret("AISSTREAM_API_KEY")
        try:
            with SingleInstance(ccfg.lock_port):
                asyncio.run(run(ccfg, api_key, duration_s=args.duration_s))
        except KeyboardInterrupt:
            logging.getLogger("cableguard").info("stopped by user")
        except RuntimeError as exc:
            logging.getLogger("cableguard").error("%s", exc)
            return 1
        return 0

    if args.cmd == "compact":
        from cableguard.collector.client import Collector
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        Collector(ccfg).compact()
        return 0

    if args.cmd == "status":
        from cableguard.collector.status import print_status
        print_status(ccfg)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
