#!/usr/bin/env python3
# Status: experimental
# Path: lib/debate/cooperative_debate.py — imported by orchestrator.py (via lib.debate)
"""CooperativeDebate — Proposer/Refuter on Azure spot VMs, Judge/Synthesis local.

Extends LocalDebate with spot VM orchestration and remote model tunneling.
"""
import os
from typing import Optional

from lib.infra.azure_spot import (
    NEMOTRON_SPOT_CONFIG,
    QWEN_SPOT_CONFIG,
    SpotOrchestrator,
    close_spot_tunnel,
    open_spot_tunnel,
)
from .cooperative_remote import close_all_tunnels, remote_activate, remote_deactivate
from .debate_data import MODELS
from .debate_llm import switch_local_model
from .local_debate import LocalDebate


class CooperativeDebate(LocalDebate):
    """Multi-agent debate — spot VM Proposer+Refuter, local Judge+Synthesis."""

    def __init__(self, question: str, method: str = "drag", skip_drag: bool = False,
                 dry_run: bool = False, reuse_vms: bool = False):
        super().__init__(question, method, skip_drag, dry_run)
        self.mode = "cooperative"
        self.reuse_vms = reuse_vms

        # Cooperative model assignments
        # inference (:8081): Qwen3-30B — DRAG + Synthesis
        self.drag_model = "qwen3-30b-a3b-local"
        self.synthesizer_model = "qwen3-30b-a3b-local"
        # Azure spot VMs (provisioned on demand, terminated on consensus)
        self.proposer_model = "qwen3-30b-a3b"          # azureqwen :8086
        self.refuter_model = "nemotron3-nano-30b"      # azurenemo :8085
        # Azure Judge VM (persistent, :8087) — Judge + Summary
        self.judge_model = "gemma-4-26b"
        self.summary_model = "gemma-4-26b"

        # Spot VM state
        self._spot_orch: Optional[SpotOrchestrator] = None
        self._spot_tunnel_ports: list = []

    # ── Model switching (local + remote) ────────────────────────────────

    def switch_model(self, model_id: str) -> bool:
        """Ensure model is ready — local supervisor or remote spot activation."""
        cfg = MODELS[model_id]
        host = cfg.get("host", "local")

        if self.dry_run:
            loc = f"remote {host}:{cfg.get('local_port', cfg['port'])}" if host != "local" else f":{cfg['port']}"
            print(f"  [dry-run] switch to {model_id} ({cfg['filename']}) on {loc}")
            return True

        if host == "local":
            return switch_local_model(model_id, dry_run=False)
        return remote_activate(model_id, self._tunnels_open)

    # ── Spot VM lifecycle ───────────────────────────────────────────────

    def _provision_spot_vms(self) -> bool:
        """Create spot VMs for Proposer (Qwen) and Refuter (Nemotron)."""
        print(f"\n{'─'*40}")
        print("  [spot] Provisioning spot VMs from golden images...")
        print(f"{'─'*40}")

        if self.dry_run:
            print("  [spot] DRY RUN — would create: qwen3-30b + nemotron3-nano spot VMs")
            return True

        try:
            orch = SpotOrchestrator(self.session_id)
            orch.add("qwen", QWEN_SPOT_CONFIG)
            orch.add("nemotron", NEMOTRON_SPOT_CONFIG)
            if not orch.provision_all():
                print("  [spot] ERROR: Spot VM provisioning failed")
                return False
            self._spot_orch = orch
            return True
        except Exception as e:
            print(f"  [spot] ERROR: {e}")
            return False

    def _open_spot_tunnels(self) -> bool:
        """Open SSH tunnels to spot VM IPs on the same local ports."""
        if self.dry_run:
            print("  [spot] DRY RUN — would open tunnels to spot VMs")
            return True

        if not self._spot_orch:
            print("  [spot] No orchestrator — cannot open tunnels")
            return False

        ssh_keys = {
            "qwen": os.path.expanduser("~/.ssh/vm-azure-qwen-30B-A3B-key.pem"),
            "nemotron": os.path.expanduser("~/.ssh/vm-azure-nvidia-nemotron3-nano-30B-key.pem"),
        }
        tunnel_map = {
            "qwen": ("qwen3-30b-a3b-local", 8086, "azureqwen"),
            "nemotron": ("nemotron3-nano-30b", 8085, "azurenemo"),
        }

        for label, (model_key, local_port, host_name) in tunnel_map.items():
            mgr = self._spot_orch.managers.get(label)
            if not mgr or not mgr.cfg.public_ip:
                print(f"  [spot] No IP for {label} — skipping tunnel")
                return False

            key_path = ssh_keys[label]
            ip = mgr.cfg.public_ip
            if not open_spot_tunnel(label, ip, 400, local_port, key_path):
                print(f"  [spot] Tunnel failed for {label}")
                return False
            self._spot_tunnel_ports.append(local_port)
            self._tunnels_open.add(host_name)

        return True

    def _terminate_spot_vms(self) -> None:
        """Close spot tunnels and delete all spot VMs."""
        for port in self._spot_tunnel_ports:
            close_spot_tunnel(port)
        self._spot_tunnel_ports.clear()

        if self._spot_orch:
            print(f"\n{'─'*40}")
            print("  [spot] Terminating spot VMs...")
            print(f"{'─'*40}")
            self._spot_orch.terminate_all()
            self._spot_orch = None
        elif not self.dry_run:
            print("  [spot] No spot VMs to terminate")

    def _reuse_existing_vms(self) -> bool:
        """Scan Azure for already-running spot VMs matching config labels."""
        print(f"\n{'─'*40}")
        print("  [spot] Reusing existing spot VMs...")
        print(f"{'─'*40}")

        if self.dry_run:
            print("  [spot] DRY RUN — would scan for existing VMs")
            return True

        try:
            orch = SpotOrchestrator(self.session_id)
            orch.add("qwen", QWEN_SPOT_CONFIG)
            orch.add("nemotron", NEMOTRON_SPOT_CONFIG)

            for label, mgr in orch.managers.items():
                # List VMs in resource group matching the label prefix
                result = mgr._az_with_sub([
                    "vm", "list",
                    "--resource-group", mgr.cfg.resource_group,
                    "--query", f"[?starts_with(name, 'spot-{mgr.cfg.label[:6]}')].{{Name:name}}",
                    "-o", "tsv",
                ], timeout=30)
                if result.returncode != 0 or not result.stdout.strip():
                    print(f"  [spot] ERROR: No existing VM found for {label}")
                    return False
                vm_name = result.stdout.strip().splitlines()[0]
                mgr.cfg.vm_name = vm_name
                mgr._nic_name = f"{vm_name}VMNic"
                mgr._pip_name = f"{vm_name}PublicIP"
                ip = mgr._get_public_ip()
                if not ip:
                    print(f"  [spot] ERROR: Cannot resolve IP for {label} ({vm_name})")
                    return False
                mgr.cfg.public_ip = ip
                print(f"  [spot] Found {label}: {vm_name} → {ip}")
            self._spot_orch = orch
            return True
        except Exception as e:
            print(f"  [spot] ERROR: {e}")
            return False

    # ── Hook overrides ──────────────────────────────────────────────────

    def _pre_dart_hook(self) -> bool:
        """Provision (or reuse) spot VMs and open tunnels before DART rounds."""
        if self.reuse_vms:
            if not self._reuse_existing_vms():
                return False
        else:
            if not self._provision_spot_vms():
                self._terminate_spot_vms()
                return False
        if not self._open_spot_tunnels():
            if not self.reuse_vms:
                self._terminate_spot_vms()
            return False
        return True

    def _post_dart_hook(self) -> None:
        """Terminate spot VMs after DART rounds (skip if reusing)."""
        if self.reuse_vms:
            for port in self._spot_tunnel_ports:
                close_spot_tunnel(port)
            self._spot_tunnel_ports.clear()
            return
        self._terminate_spot_vms()

    def _post_summary_hook(self) -> None:
        """Close Judge/Gemma tunnel after summary — not needed for synthesis."""
        if not self.dry_run:
            remote_deactivate(self.judge_model)
            self._tunnels_open.discard("azuregemma")

    def _cleanup_hook(self) -> None:
        """Close all SSH tunnels after synthesis."""
        if not self.dry_run:
            close_all_tunnels(self._tunnels_open)

    # ── Header override ─────────────────────────────────────────────────

    def _print_header(self) -> None:
        print(f"\n{'█'*60}")
        print(f"█ DevForge Multi-Agent LLM Debate v4.6 ({self.mode})")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        print("█ Spot VMs → P:Qwen3-30B(:8086) R:Nemotron(:8085) | J+S:Gemma4-26B(:8087) | DRAG+Synth: Qwen3-30B(:8081)")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█'*60}")

