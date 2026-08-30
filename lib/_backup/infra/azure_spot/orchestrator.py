#!/usr/bin/env python3
# Status: experimental
"""Azure Spot VM orchestrator — coordinates VM lifecycle for debate rounds."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from lib.infra.azure_spot.config import SPOT_CONFIGS, SpotVMConfig
from lib.infra.azure_spot.manager import SpotVMManager
from lib.infra.azure_spot.tunnel import open_spot_tunnel


class SpotOrchestrator:
    def __init__(self, configs: Optional[List[SpotVMConfig]] = None):
        self.configs = configs or list(SPOT_CONFIGS.values())
        self.vms: Dict[str, Dict[str, Any]] = {}
        self.tunnels: Dict[str, Any] = {}

    def launch_all(self, ssh_timeout: int = 180, llm_timeout: int = 300) -> Dict[str, str]:
        results: Dict[str, str] = {}
        for cfg in self.configs:
            mgr = SpotVMManager(cfg)
            try:
                vm = mgr.create_vm()
            except RuntimeError as e:
                print(f"  Failed to create VM for {cfg.label}: {e}")
                results[cfg.label] = "failed"
                continue
            self.vms[cfg.label] = vm

            if not mgr.poll_until_ready(vm["ip"], ssh_timeout, llm_timeout):
                results[cfg.label] = "unhealthy"
                continue

            local_port = 8081 + len(self.tunnels)
            proc = open_spot_tunnel(cfg.label, vm["ip"], 8081, local_port)
            if proc:
                self.tunnels[cfg.label] = {"proc": proc, "port": local_port, "ip": vm["ip"]}
            results[cfg.label] = vm["ip"]
            time.sleep(5)
        return results

    def close_all_tunnels(self):
        from lib.infra.azure_spot.tunnel import close_spot_tunnel
        for label, info in self.tunnels.items():
            print(f"  Closing tunnel for {label}")
            close_spot_tunnel(info["port"])

    def delete_all_vms(self):
        for label, vm in self.vms.items():
            cfg = SPOT_CONFIGS.get(label)
            if cfg:
                SpotVMManager(cfg).delete_vm(vm["name"])

    def cleanup_all(self):
        self.close_all_tunnels()
        self.delete_all_vms()
