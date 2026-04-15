"""eSCL (AirScan/Mopria) protocol client for HTTP-based scanner communication.

Works with any eSCL-compatible scanner: Canon, HP, Epson, Brother,
Xerox, Ricoh, Kyocera, Lexmark, Samsung, Konica Minolta, etc.

Protocol reference: PWG Candidate Standard 5100.15 (eSCL).
The workflow is: POST /ScanJobs -> GET /NextDocument in a loop -> DELETE job.
"""

from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import io

import httpx
from PIL import Image

from auto_scan import ScanError, ScannerBusyError

# ── XML namespace constants ──────────────────────────────────────────
# eSCL uses two XML namespaces: one from the PWG standard (scanner-agnostic),
# and one HP-originated namespace that became the de facto eSCL standard.
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
    """Parameters for a single scan job, serialized to XML for the POST body."""

    source: str = "Feeder"       # "Feeder" (ADF) or "Platen" (flatbed glass)
    color_mode: str = "RGB24"    # RGB24, Grayscale8, or BlackAndWhite1
    resolution: int = 300        # DPI — 300 is a good default for OCR
    document_format: str = "image/jpeg"

    def to_xml(self) -> str:
        # Build XML via ElementTree to prevent injection through field values.
        # Using ET instead of f-strings avoids XML injection if field values
        # ever come from user input (e.g. a web UI).
        root = ET.Element("scan:ScanSettings", {
            "xmlns:pwg": PWG_NS,
            "xmlns:scan": SCAN_NS,
        })
        ET.SubElement(root, "pwg:Version").text = "2.0"
        ET.SubElement(root, "pwg:InputSource").text = str(self.source)
        ET.SubElement(root, "scan:ColorMode").text = str(self.color_mode)
        ET.SubElement(root, "scan:XResolution").text = str(int(self.resolution))
        ET.SubElement(root, "scan:YResolution").text = str(int(self.resolution))
        ET.SubElement(root, "pwg:DocumentFormat").text = str(self.document_format)
        ET.SubElement(root, "scan:Intent").text = "Document"
        return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")


# ── Format normalization ─────────────────────────────────────────────
# Scanners are unreliable about honoring DocumentFormat. Canon GX7050, for
# example, sometimes returns PDF even when JPEG was requested. We normalize
# everything to JPEG so downstream code (img2pdf, dedup hashing) can rely
# on a single format.

def _ensure_jpeg(data: bytes) -> bytes:
    """Convert scanner output to JPEG if it isn't already.

    Some scanners (notably Canon) may return PDF or other formats even
    when JPEG was requested. This normalizes everything to JPEG bytes.
    """
    # Fast path: JPEG magic bytes (FF D8)
    if data[:2] == b"\xff\xd8":
        return data

    # Try opening as a regular image (TIFF, PNG, BMP, etc.)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        pass

    # Try extracting image from PDF (%PDF- magic bytes)
    if data[:5] == b"%PDF-":
        try:
            import pikepdf
            pdf = pikepdf.open(io.BytesIO(data))
            page = pdf.pages[0]
            for key in page.images:
                pil_img = page.images[key].as_pil_image()
                buf = io.BytesIO()
                pil_img.convert("RGB").save(buf, format="JPEG", quality=92)
                pdf.close()
                return buf.getvalue()
            pdf.close()
        except Exception:
            pass

    raise ScanError(
        "Scanner returned an unrecognized image format. "
        "Check that your scanner supports JPEG output."
    )


# ── eSCL HTTP client ─────────────────────────────────────────────────

class ESCLClient:
    """HTTP client for the eSCL scanning protocol."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        # TLS verification disabled: virtually all network scanners ship with
        # self-signed certs and there is no practical CA chain to validate.
        # Trade-off: MITM is possible on the local network, but requiring
        # valid certs would make the tool unusable with most scanners.
        self._client = httpx.Client(verify=False, timeout=30.0)
        if base_url.startswith("https"):
            print(
                "  Note: TLS certificate verification is disabled for scanner connection "
                "(self-signed certs are common on network scanners).",
                file=sys.stderr,
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ESCLClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Capability & status queries ────────────────────────────────────

    def get_capabilities(self) -> ScannerCapabilities:
        """Fetch scanner capabilities (supported resolutions, modes, etc.)."""
        resp = self._client.get(f"{self.base_url}/ScannerCapabilities")
        resp.raise_for_status()

        root = ET.fromstring(resp.text)

        # Walk the entire XML tree: capability XML structure varies between
        # vendors, but tag names are consistent, so iter() is more robust
        # than relying on a fixed path.
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
        # Presence of <Platen> or <Adf> elements indicates physical source availability
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

        # dict.fromkeys preserves order while deduplicating
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

    # ── Scan job execution ────────────────────────────────────────────

    def scan(self, settings: ScanSettings, on_page=None, cancel_fn=None) -> list[bytes]:
        """Execute a scan job and return a list of page images (JPEG bytes).

        For ADF scanning, loops until all pages are consumed.
        For flatbed, returns a single page.

        Args:
            on_page: Optional callback called with page count after each page is scanned.
            cancel_fn: Optional callable returning True if the scan should stop.
                       Checked between pages — the current page always completes.
                       Returns whatever pages were captured so far (may be empty).
        """
        # ── Step 1: Create the scan job (POST /ScanJobs) ──
        resp = self._client.post(
            f"{self.base_url}/ScanJobs",
            content=settings.to_xml(),
            headers={"Content-Type": "text/xml"},
        )

        # 409 Conflict = another job is already running on this scanner
        if resp.status_code == 409:
            raise ScannerBusyError("Scanner is busy with another job.")
        if resp.status_code != 201:
            raise ScanError(
                f"Failed to create scan job: HTTP {resp.status_code}\n{resp.text}"
            )

        job_url = resp.headers.get("Location", "")
        if not job_url:
            raise ScanError("Scanner did not return a job URL.")

        # Normalize job URL: the eSCL spec says Location should be absolute,
        # but some scanners (observed on Canon GX7050) return a relative path.
        if job_url.startswith("/"):
            parts = self.base_url.split("/")
            host = "/".join(parts[:3])  # "http(s)://host:port"
            job_url = host + job_url

        # ── Step 2: Retrieve pages in a loop (GET /NextDocument) ──
        print("Scanning...", file=sys.stderr)
        pages: list[bytes] = []
        max_retries = 3
        cancelled = False

        while True:
            # Cancel is cooperative: checked between pages so the mechanical
            # scan of the current sheet always finishes (can't retract paper).
            if cancel_fn and cancel_fn():
                cancelled = True
                print(f"  Scan stopped by user after {len(pages)} page(s)", file=sys.stderr)
                break

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

            # 404/410 = job complete, no more pages in the ADF
            if page_resp.status_code in (404, 410):
                break
            # 503 = scanner still processing the previous page. Common on
            # Canon and HP ADF scanners: the hardware needs a moment between
            # sheets. Retry after a brief delay rather than treating as error.
            if page_resp.status_code == 503:
                retries += 1
                if retries >= max_retries:
                    break
                time.sleep(2)
                continue
            if page_resp.status_code != 200:
                raise ScanError(
                    f"Error retrieving page: HTTP {page_resp.status_code}"
                )

            pages.append(_ensure_jpeg(page_resp.content))
            print(f"  Page {len(pages)} scanned", file=sys.stderr)
            if on_page:
                on_page(len(pages))

            # Flatbed (Platen) has no feeder — only one page is possible
            if settings.source == "Platen":
                break

        if not pages and not cancelled:
            raise ScanError(
                "No pages were scanned. Check that documents are loaded in the feeder."
            )

        # ── Step 3: Clean up (DELETE job) ──
        # Best-effort: some scanners auto-delete jobs, and failure here is harmless.
        try:
            self._client.delete(job_url)
        except httpx.HTTPError:
            pass

        if cancelled:
            print(f"Scan stopped: {len(pages)} page(s) captured", file=sys.stderr)
        else:
            print(f"Scan complete: {len(pages)} page(s)", file=sys.stderr)
        return pages
