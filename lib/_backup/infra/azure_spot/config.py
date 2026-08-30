#!/usr/bin/env python3
# Status: experimental
"""Azure Spot VM configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

SUBSCRIPTIONS: Dict[str, str] = {
    "account1": "a942e898-e1ee-47f4-b9b3-d9475672ff4e",
    "account2": "e71711e2-1f81-4399-b22f-90e905a82fb6",
    "account3": "d0a7db48-f2a5-4f94-959c-f836afde4f86",
}

RESOURCE_GROUP = "devforge-spot-vms"
LOCATION = "centralindia"
VNET_NAME = "spot-vnet"
SUBNET_NAME = "spot-subnet"
NSG_NAME = "spot-nsg"
PUBLIC_IP_SKU = "Standard"
VM_SIZE = "Standard_NC24ads_A100_v4"
SSH_USER = "azureuser"
LLAMA_SERVER_PORT = 8081
SSH_KEY_PATH = "~/.ssh/id_rsa.pub"
LLAMA_SERVER_PORT = 8081


@dataclass
class SpotVMConfig:
    label: str
    subscription_id: str
    gallery_image: str
    location: str = LOCATION
    vm_size: str = VM_SIZE
    account: str = ""

    def image_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{RESOURCE_GROUP}/providers"
            f"/Microsoft.Compute/galleries/devforge/images/{self.gallery_image}/versions/latest"
        )


QWEN_SPOT_CONFIG = SpotVMConfig(
    label="qwen3-30b",
    subscription_id=SUBSCRIPTIONS["account1"],
    gallery_image="img-qwen3-30b-a3b-v3",
    account="account1",
)

NEMOTRON_SPOT_CONFIG = SpotVMConfig(
    label="nemotron3-nano",
    subscription_id=SUBSCRIPTIONS["account2"],
    gallery_image="img-nemotron3-nano-30b-spot",
    location="indonesiacentral",
    account="account2",
)

GEMMA_SPOT_CONFIG = SpotVMConfig(
    label="gemma-4-26b",
    subscription_id=SUBSCRIPTIONS["account3"],
    gallery_image="img-gemma-4-26b-spot",
    account="account3",
)

SPOT_CONFIGS: Dict[str, SpotVMConfig] = {
    "qwen3-30b": QWEN_SPOT_CONFIG,
    "nemotron3-nano": NEMOTRON_SPOT_CONFIG,
    "gemma-4-26b": GEMMA_SPOT_CONFIG,
}
