"""Shared aiohttp session construction.

Exists for one reason: aiohttp picks its DNS resolver at import time, and the
choice is wrong on Windows whenever aiodns is installed.

When aiodns/pycares is present, aiohttp's DefaultResolver becomes AsyncResolver,
which uses c-ares. c-ares does not read the Windows DNS configuration reliably
and fails every lookup with:

    ClientConnectorDNSError: ... [Could not contact DNS servers]

even though the OS resolves the same hostname fine. That produced what looked
like a network outage across every REST call in this project - including the
Week 1 funding-rate backfill, whose WebSocket connections succeeded because the
websockets library resolves through a different path.

ThreadedResolver delegates to the OS getaddrinfo in a thread pool. Slightly less
efficient than c-ares, entirely irrelevant at our request rate, and correct.
"""
import aiohttp
from aiohttp.resolver import ThreadedResolver


def make_session(**kwargs) -> aiohttp.ClientSession:
    """
    Build a ClientSession with a resolver that works on Windows.

    Use this rather than constructing ClientSession directly, so the resolver
    choice stays consistent across the codebase.
    """
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    return aiohttp.ClientSession(connector=connector, **kwargs)
