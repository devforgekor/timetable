#!/usr/bin/env python3
# Status: experimental
"""Azure Spot VM lifecycle management.

Sub-modules:
  config        — SpotVMConfig, subscription map, pre-defined configs
  manager       — SpotVMManager (create, poll, delete VMs)
  orchestrator  — SpotOrchestrator (multi-VM launch + tunnel management)
  tunnel        — SSH tunnel open/close
  cli           — Command-line entry point
"""

from lib.infra.azure_spot.config import (
    SUBSCRIPTIONS, RESOURCE_GROUP, LOCATION, VM_SIZE, SSH_USER, LLAMA_SERVER_PORT,
    SpotVMConfig, QWEN_SPOT_CONFIG, NEMOTRON_SPOT_CONFIG, GEMMA_SPOT_CONFIG, SPOT_CONFIGS,
)
from lib.infra.azure_spot.manager import SpotVMManager
from lib.infra.azure_spot.orchestrator import SpotOrchestrator
from lib.infra.azure_spot.tunnel import open_spot_tunnel, close_spot_tunnel
from lib.infra.azure_spot.cli import main
