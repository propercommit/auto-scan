"""auto-scan: Scan documents from any network scanner and auto-classify with AI."""


class AutoScanError(Exception):
    """Base exception for auto-scan."""


class ScannerNotFoundError(AutoScanError):
    """Scanner could not be discovered on the network."""


class ScannerBusyError(AutoScanError):
    """Scanner is currently busy with another job."""


class ScanError(AutoScanError):
    """Error during the scanning process."""


class AnalysisError(AutoScanError):
    """Error during document analysis with Claude."""
