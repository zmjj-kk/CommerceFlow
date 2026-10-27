from enum import StrEnum


class Capability(StrEnum):
    PUBLIC_READ = "public_read"
    SENSITIVE_READ = "sensitive_read"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_EFFECT = "external_effect"
    NETWORK = "network"
