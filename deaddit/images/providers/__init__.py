"""Concrete image-provider adapters (fal.ai, Runware, ...).

Importing this package performs no I/O and registers nothing; callers wire a
provider's adapter into deaddit.images.client.register_adapter() explicitly
(production app setup or tests).
"""
