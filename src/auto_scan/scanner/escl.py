"""eSCL (AirScan) protocol client for HTTP-based scanner communication."""

from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from auto_scan import ScanError, ScannerBusyError

PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"
SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"

NS = {"pwg": PWG_NS, "scan": SCAN_NS}


@dataclass
class ScannerCapabilities:
    resolutions: list[int]
    color_modes: list[str]
    sources: list[str]
    formats: list[str]


@dataclass
class ScannerStatus:
    state: str  # Idle, Processing, Stopped
    adf_state: str | None  # ScannerAdfLoaded, ScannerAdfEmpty, etc.


@dataclass
class ScanSettings:
    source: str = "Feeder"
    color_mode: str = "RGB24"
    resolution: int = 300
    document_format: str = "image/jpeg"

    def to_xml(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<scan:ScanSettings xmlns:pwg="{PWG_NS}" xmlns:scan="{SCAN_NS}">'
            "<pwg:Version>2.0</pwg:Version>"
            f"<pwg:InputSource>{self.source}</pwg:InputSource>"
            f"<scan:ColorMode>{self.color_mode}</scan:ColorMode>"
            f"<scan:XResolution>{self.resolution}</scan:XResolution>"
            f"<scan:YResolution>{self.resolution}</scan:YResolution>"
            f"<pwg:DocumentFormat>{self.document_format}</pwg:DocumentFormat>"
            "<scan:Intent>Document</scan:Intent>"
            "</scan:ScanSettings>"
        )


class ESCLClient:
    """HTTP client for the eSCL scanning protocol."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(verify=False, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ESCLClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_capabilities(self) -> ScannerCapabilities:
        """Fetch scanner capabilities (supported resolutions, modes, etc.)."""
        resp = self._client.get(f"{self.base_url}/ScannerCapabilities")
        resp.raise_for_status()

        root = ET.fromstring(resp.text)

        resolutions = sorted(
            {
                int(el.text)
                for el in root.iter()
                if el.tag.endswith("XResolution") and el.text
            }
        )
        color_modes = [
            el.text
            for el in root.iter()
            if el.tag.endswith("ColorMode") and el.text
        ]
        sources = []
        if root.find(f".//{{{SCAN_NS}}}Platen") is not None:
            sources.append("Platen")
        if root.find(f".//{{{SCAN_NS}}}Adf") is not None:
            sources.append("Feeder")
        formats = [
            el.text
            for el in root.iter()
            if el.tag.endswith("DocumentFormat") and el.text
        ]

        return ScannerCapabilities(
            resolutions=resolutions,
            color_modes=list(dict.fromkeys(color_modes)),
            sources=sources,
            formats=list(dict.fromkeys(formats)),
        )

    def get_status(self) -> ScannerStatus:
        """Check current scanner state and ADF status."""
        resp = self._client.get(f"{self.base_url}/ScannerStatus")
        resp.raise_for_status()

        root = ET.fromstring(resp.text)

        state_el = root.find(f".//{{{PWG_NS}}}State")
        state = state_el.text if state_el is not None and state_el.text else "Unknown"

        adf_el = root.find(f".//{{{SCAN_NS}}}AdfState")
        adf_state = adf_el.text if adf_el is not None else None

        return ScannerStatus(state=state, adf_state=adf_state)

    def scan(self, settings: ScanSettings) -> list[bytes]:
        """Execute a scan job and return a list of page images (JPEG bytes).

        For ADF scanning, loops until all pages are consumed.
        For flatbed, returns a single page.
        """
        # Create the scan job
        resp = self._client.post(
            f"{self.base_url}/ScanJobs",
            content=settings.to_xml(),
            headers={"Content-Type": "text/xml"},
        )

        if resp.status_code == 409:
            raise ScannerBusyError("Scanner is busy with another job.")
        if resp.status_code != 201:
            raise ScanError(
                f"Failed to create scan job: HTTP {resp.status_code}\n{resp.text}"
            )

        job_url = resp.headers.get("Location", "")
        if not job_url:
            raise ScanError("Scanner did not return a job URL.")

        # Normalize job URL — some scanners return relative paths
        if job_url.startswith("/"):
            # Extract scheme + host from base_url
            parts = self.base_url.split("/")
            host = "/".join(parts[:3])
            job_url = host + job_url

        print("Scanning...", file=sys.stderr)
        pages: list[bytes] = []
        max_retries = 3

        while True:
            retries = 0
            while retries < max_retries:
                try:
                    page_resp = self._client.get(
                        f"{job_url}/NextDocument", timeout=120.0
                    )
                    break
                except httpx.ReadTimeout:
                    retries += 1
                    if retries >= max_retries:
                        raise ScanError("Timed out waiting for scanner.")
                    time.sleep(2)

            if page_resp.status_code in (404, 410):
                # No more pages
                break
            if page_resp.status_code == 503:
                # Canon devices may return 503 between ADF pages
                retries += 1
                if retries >= max_retries:
                    break
                time.sleep(2)
                continue
            if page_resp.status_code != 200:
                raise ScanError(
                    f"Error retrieving page: HTTP {page_resp.status_code}"
                )

            pages.append(page_resp.content)
            print(f"  Page {len(pages)} scanned", file=sys.stderr)

            # Flatbed only scans one page
            if settings.source == "Platen":
                break

        if not pages:
            raise ScanError(
                "No pages were scanned. Check that documents are loaded in the feeder."
            )

        # Clean up the job
        try:
            self._client.delete(job_url)
        except httpx.HTTPError:
            pass  # Best-effort cleanup

        print(f"Scan complete: {len(pages)} page(s)", file=sys.stderr)
        return pages
