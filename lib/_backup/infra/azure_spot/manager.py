#!/usr/bin/env python3
# Status: experimental
"""Azure Spot VM manager — create, poll, delete spot VMs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List

from lib.infra.azure_spot.config import (
    LLAMA_SERVER_PORT,
    RESOURCE_GROUP,
    SSH_KEY_PATH,
    SSH_USER,
    SpotVMConfig,
)


def _az(*args: str, subscription: str = "") -> subprocess.CompletedProcess:
    cmd = ["az"]
    if subscription:
        cmd += ["--subscription", subscription]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _vm_name(label: str) -> str:
    return f"spot-{label}-{int(time.time())}"


def _wait_for_ssh(ip: str, timeout: int = 180, interval: int = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=5",
                f"{SSH_USER}@{ip}",
                "echo ssh_ok",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and "ssh_ok" in r.stdout:
            return True
        time.sleep(interval)
    return False


def _wait_for_inference_server(
    ip: str, port: int = 8081, timeout: int = 300, interval: int = 15
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                f"{SSH_USER}@{ip}",
                f"curl -s http://localhost:{port}/health | head -c 200",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "ok" in r.stdout.lower() or "healthy" in r.stdout.lower():
            return True
        time.sleep(interval)
    return False


class SpotVMManager:
    def __init__(self, config: SpotVMConfig):
        self.config = config

    def create_vm(self, vm_name: str | None = None) -> Dict[str, Any]:
        cfg = self.config
        name = vm_name or _vm_name(cfg.label)

        print(f"  Creating spot VM '{name}' in {cfg.location}...")
        r = _az(
            "vm",
            "create",
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            name,
            "--image",
            cfg.image_id(),
            "--size",
            cfg.vm_size,
            "--location",
            cfg.location,
            "--vnet-name",
            "spot-vnet",
            "--subnet",
            "spot-subnet",
            "--public-ip-sku",
            "Standard",
            "--security-type",
            "Standard",
            "--priority",
            "Spot",
            "--eviction-policy",
            "Delete",
            "--max-price",
            "0.05",
            "--admin-username",
            SSH_USER,
            "--ssh-key-values",
            os.path.expanduser(SSH_KEY_PATH),
            "--nic-delete-option",
            "Delete",
            "--os-disk-delete-option",
            "Delete",
            "--data-disk-delete-option",
            "Delete",
            "--storage-sku",
            "StandardSSD_LRS",
            "--os-disk-size-gb",
            "64",
            subscription=cfg.subscription_id,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create VM '{name}': {r.stderr}")

        vm_info = json.loads(r.stdout)
        ip = vm_info.get("publicIpAddress") or self._get_ip(name)
        print(f"  VM '{name}' created, IP={ip}")
        return {"name": name, "ip": ip}

    def _get_ip(self, vm_name: str) -> str:
        r = _az(
            "vm",
            "show",
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            vm_name,
            "--query",
            "publicIpAddress",
            "--output",
            "tsv",
            subscription=self.config.subscription_id,
        )
        return r.stdout.strip()

    def poll_until_ready(self, ip: str, ssh_timeout: int = 180, llm_timeout: int = 300) -> bool:
        print(f"  Waiting for SSH on {ip} (timeout={ssh_timeout}s)...")
        if not _wait_for_ssh(ip, timeout=ssh_timeout):
            print(f"  SSH not available on {ip} within {ssh_timeout}s")
            return False

        print(f"  Waiting for llama-server on {ip}:{LLAMA_SERVER_PORT} (timeout={llm_timeout}s)...")
        if not _wait_for_inference_server(ip, timeout=llm_timeout):
            print(f"  llama-server not ready on {ip} within {llm_timeout}s")
            return False

        print(f"  VM {ip} ready for inference")
        return True

    def delete_vm(self, vm_name: str) -> bool:
        print(f"  Deleting spot VM '{vm_name}'...")
        r = _az(
            "vm",
            "delete",
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            vm_name,
            "--yes",
            subscription=self.config.subscription_id,
        )
        if r.returncode != 0:
            print(f"  Failed to delete VM '{vm_name}': {r.stderr}")
            return False
        print(f"  VM '{vm_name}' deleted")
        return True

    def list_spot_vms(self) -> List[Dict[str, str]]:
        r = _az(
            "vm",
            "list",
            "--resource-group",
            RESOURCE_GROUP,
            "--query",
            "[?priority=='Spot'].{name:name, vmId:id, powerState:powerState}",
            "--output",
            "json",
            subscription=self.config.subscription_id,
        )
        return json.loads(r.stdout) if r.stdout.strip() else []
