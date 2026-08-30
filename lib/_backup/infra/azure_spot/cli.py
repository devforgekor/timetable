#!/usr/bin/env python3
"""CLI entry point for Azure Spot VM lifecycle management."""

from __future__ import annotations

import argparse
import sys

from lib.infra.azure_spot.config import SPOT_CONFIGS, SpotVMConfig
from lib.infra.azure_spot.manager import SpotVMManager
from lib.infra.azure_spot.orchestrator import SpotOrchestrator
from lib.infra.azure_spot.tunnel import close_spot_tunnel


def main():
    parser = argparse.ArgumentParser(description="Azure Spot VM lifecycle management")
    sub = parser.add_subparsers(dest="command")

    launch_p = sub.add_parser("launch", help="Launch spot VMs")
    launch_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), help="Specific VM label")
    launch_p.add_argument("--ssh-timeout", type=int, default=180)
    launch_p.add_argument("--llm-timeout", type=int, default=300)

    status_p = sub.add_parser("status", help="List running VMs")
    status_p.add_argument("--label", choices=list(SPOT_CONFIGS.keys()), default="all")

    delete_p = sub.add_parser("delete", help="Delete a specific VM")
    delete_p.add_argument("label", choices=list(SPOT_CONFIGS.keys()))
    delete_p.add_argument("name", help="VM name to delete")

    sub.add_parser("list-tunnels", help="List active tunnels")

    args = parser.parse_args()

    if args.command == "launch":
        configs = [SPOT_CONFIGS[args.label]] if args.label else None
        orch = SpotOrchestrator(configs)
        results = orch.launch_all(ssh_timeout=args.ssh_timeout, llm_timeout=args.llm_timeout)
        for label, status in results.items():
            print(f"  {label}: {status}")
        return 0

    elif args.command == "status":
        if args.label != "all":
            configs = [SPOT_CONFIGS[args.label]]
        else:
            configs = SPOT_CONFIGS.values()
        for cfg in configs:
            mgr = SpotVMManager(cfg)
            vms = mgr.list_spot_vms()
            for vm in vms:
                print(f"  {cfg.label}: {vm['name']} ({vm.get('powerState', 'unknown')})")
        return 0

    elif args.command == "delete":
        cfg = SPOT_CONFIGS[args.label]
        SpotVMManager(cfg).delete_vm(args.name)
        return 0

    elif args.command == "list-tunnels":
        print("  (implement via lsof or process list)")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
