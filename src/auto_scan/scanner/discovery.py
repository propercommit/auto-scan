from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

from auto_scan import ScannerNotFoundError

ESCL_SERVICE_TYPE = "_uscan._tcp.local."


@dataclass
class ScannerInfo:
    ip: str
    port: int
    root_path: str
    name: str

    @property
    def base_url(self) -> str:
        scheme = "https" if self.port == 443 else "http"
        return f"{scheme}://{self.ip}:{self.port}{self.root_path}"


class _ScannerListener:
    """mDNS listener that stops when a Canon scanner is found."""

    def __init__(self) -> None:
        self.found: ScannerInfo | None = None
        self.event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return

        props = {
            k.decode(): v.decode() if isinstance(v, bytes) else v
            for k, v in info.properties.items()
        }
        device_type = props.get("ty", "")

        if "canon" in device_type.lower() or "canon" in name.lower():
            addresses = info.parsed_addresses()
            if not addresses:
                return
            self.found = ScannerInfo(
                ip=addresses[0],
                port=info.port,
                root_path=props.get("rs", "/eSCL"),
                name=device_type or name,
            )
            self.event.set()

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover_scanner(timeout: float = 5.0) -> ScannerInfo:
    """Find a Canon eSCL scanner on the local network via mDNS.

    Raises ScannerNotFoundError if no scanner is found within the timeout.
    """
    print("Searching for Canon scanner on the network...", file=sys.stderr)

    zc = Zeroconf()
    listener = _ScannerListener()
    browser = ServiceBrowser(zc, ESCL_SERVICE_TYPE, listener)

    try:
        listener.event.wait(timeout=timeout)
    finally:
        browser.cancel()
        zc.close()

    if listener.found is None:
        raise ScannerNotFoundError(
            "Could not find a Canon scanner on the network.\n"
            "Check that the scanner is powered on and connected to Wi-Fi,\n"
            "or set SCANNER_IP in your .env file to skip discovery."
        )

    print(f"Found: {listener.found.name} at {listener.found.ip}", file=sys.stderr)
    return listener.found


def scanner_info_from_ip(ip: str, port: int = 443) -> ScannerInfo:
    """Construct ScannerInfo directly from an IP address (skip discovery)."""
    return ScannerInfo(ip=ip, port=port, root_path="/eSCL", name=f"Canon@{ip}")
