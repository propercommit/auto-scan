"""mDNS discovery for eSCL (AirScan/Mopria) compatible scanners.

Supports any scanner that advertises _uscan._tcp via mDNS, including:
Canon, HP, Epson, Brother, Xerox, Ricoh, Kyocera, Lexmark, Samsung,
Konica Minolta, and other AirScan/Mopria-compatible devices.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

from auto_scan import ScannerNotFoundError

# ── mDNS service type ────────────────────────────────────────────────
# _uscan._tcp is the Bonjour/mDNS service type for eSCL scanners,
# registered by Apple for AirScan. Almost all modern network scanners
# advertise this. (There is also _uscans._tcp for TLS-only, but in
# practice scanners advertise both and _uscan is more universally present.)
ESCL_SERVICE_TYPE = "_uscan._tcp.local."


@dataclass
class ScannerInfo:
    ip: str
    port: int
    root_path: str
    name: str

    @property
    def base_url(self) -> str:
        # Port 443 strongly implies the scanner expects TLS (eSCLS).
        # The root_path (from mDNS TXT record "rs") is usually "/eSCL".
        scheme = "https" if self.port == 443 else "http"
        path = self.root_path if self.root_path.startswith("/") else f"/{self.root_path}"
        return f"{scheme}://{self.ip}:{self.port}{path}"


# ── mDNS listener ────────────────────────────────────────────────────
# Zeroconf's ServiceBrowser calls add_service on a background thread each
# time a scanner responds. We use a threading.Event so the main thread
# can block until at least one match is found (or timeout expires).

class _ScannerListener:
    """mDNS listener that collects all eSCL scanners found on the network."""

    def __init__(self, brand_filter: str | None = None) -> None:
        self.found: ScannerInfo | None = None
        self.all_scanners: list[ScannerInfo] = []
        self.brand_filter = brand_filter.lower() if brand_filter else None
        self.event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return

        # mDNS TXT records carry scanner metadata as key-value pairs.
        # "ty" = human-readable model name, "rs" = eSCL root path.
        props = {
            k.decode(): v.decode() if isinstance(v, bytes) else v
            for k, v in info.properties.items()
        }
        device_type = props.get("ty", "")

        addresses = info.parsed_addresses()
        if not addresses:
            return

        scanner = ScannerInfo(
            ip=addresses[0],
            port=info.port,
            root_path=props.get("rs", "/eSCL"),  # default per eSCL spec
            name=device_type or name,
        )
        self.all_scanners.append(scanner)

        # Brand filter: case-insensitive substring match against both the
        # model name ("ty") and the mDNS service name. This lets users say
        # --brand=canon to skip the HP printer that also speaks eSCL.
        if self.brand_filter:
            if self.brand_filter in device_type.lower() or self.brand_filter in name.lower():
                if not self.found:
                    self.found = scanner
                    self.event.set()
        else:
            if not self.found:
                self.found = scanner
                self.event.set()

    # Required by the Zeroconf listener interface but not useful here
    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


# ── Public discovery API ─────────────────────────────────────────────

def discover_scanner(
    timeout: float = 5.0,
    brand: str | None = None,
) -> ScannerInfo:
    """Find an eSCL scanner on the local network via mDNS.

    Args:
        timeout: Seconds to wait for discovery.
        brand: Optional brand filter (e.g. "canon", "hp", "epson").
               If None, the first eSCL scanner found is returned.

    Raises ScannerNotFoundError if no matching scanner is found.
    """
    msg = f"Searching for {brand or 'eSCL'} scanner on the network..."
    print(msg, file=sys.stderr)

    zc = Zeroconf()
    listener = _ScannerListener(brand_filter=brand)
    browser = ServiceBrowser(zc, ESCL_SERVICE_TYPE, listener)

    try:
        listener.event.wait(timeout=timeout)
    finally:
        browser.cancel()
        zc.close()

    if listener.found is None:
        brand_msg = f" {brand}" if brand else ""
        raise ScannerNotFoundError(
            f"Could not find a{brand_msg} scanner on the network.\n"
            "Check that the scanner is powered on and connected to Wi-Fi,\n"
            "or set SCANNER_IP in your .env file to skip discovery."
        )

    print(f"Found: {listener.found.name} at {listener.found.ip}", file=sys.stderr)
    return listener.found


def discover_all_scanners(timeout: float = 5.0) -> list[ScannerInfo]:
    """Find all eSCL scanners on the local network.

    Returns a list of ScannerInfo objects (may be empty).
    """
    print("Scanning network for all eSCL devices...", file=sys.stderr)

    zc = Zeroconf()
    listener = _ScannerListener()
    browser = ServiceBrowser(zc, ESCL_SERVICE_TYPE, listener)

    try:
        listener.event.wait(timeout=timeout)
        # After finding the first scanner, keep listening for the full timeout
        # so we collect *all* devices on the network (mDNS responses are async).
        if listener.found:
            import time
            time.sleep(max(0, timeout - 0.5))
    finally:
        browser.cancel()
        zc.close()

    print(f"Found {len(listener.all_scanners)} scanner(s)", file=sys.stderr)
    return listener.all_scanners


def scanner_info_from_ip(ip: str, port: int | None = None) -> ScannerInfo:
    """Construct ScannerInfo directly from an IP address (skip discovery).

    Used when the user sets SCANNER_IP in .env — bypasses mDNS entirely,
    which is faster and works across subnets where mDNS may not reach.

    If *port* is ``None`` (the default), probes HTTPS 443 first, then
    HTTP 80 and 8080 to find a reachable eSCL endpoint.  This avoids
    TLS handshake failures on scanners that only support plain HTTP.
    """
    if port is not None:
        return ScannerInfo(ip=ip, port=port, root_path="/eSCL", name=f"Scanner@{ip}")

    # Probe common eSCL ports to pick the right scheme automatically.
    import httpx, ssl
    from auto_scan.scanner.escl import _scanner_ssl_context

    for try_port, scheme, verify in [
        (443, "https", _scanner_ssl_context()),
        (80, "http", False),
        (8080, "http", False),
    ]:
        url = f"{scheme}://{ip}:{try_port}/eSCL/ScannerStatus"
        try:
            with httpx.Client(verify=verify, timeout=5.0) as c:
                resp = c.get(url)
                if resp.status_code < 500:
                    print(
                        f"  Scanner reachable at {scheme}://{ip}:{try_port}",
                        file=sys.stderr,
                    )
                    return ScannerInfo(
                        ip=ip, port=try_port, root_path="/eSCL",
                        name=f"Scanner@{ip}",
                    )
        except Exception:
            continue

    # None of the probes succeeded — default to 443 and let the ESCLClient
    # handle the fallback with a more detailed error.
    return ScannerInfo(ip=ip, port=443, root_path="/eSCL", name=f"Scanner@{ip}")
