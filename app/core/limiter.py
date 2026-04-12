"""
Shared slowapi rate-limiter instance.

Defined here (not in main.py) so both main.py and routers can import it
without circular dependencies.

The limiter is keyed by remote IP address.  In production, ensure that
the real client IP is propagated (e.g. trust X-Forwarded-For from a
known proxy by configuring Uvicorn's --forwarded-allow-ips).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
