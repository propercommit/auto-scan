"""GUI for auto-scan using tkinter + ttk."""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

from auto_scan import AutoScanError
from auto_scan.analyzer import DocumentInfo, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.organizer import save_document, save_unclassified
from auto_scan.scanner.discovery import ScannerInfo, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings


class AutoScanApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Auto-Scan")
        self.root.geometry("720x760")
        self.root.minsize(600, 680)

        # Use native macOS styling
        style = ttk.Style()
        style.theme_use("aqua")
        style.configure("Header.TLabel", font=("Helvetica", 13, "bold"))
        style.configure("Status.TLabel", foreground="gray")
        style.configure("StatusOK.TLabel", foreground="green")
        style.configure("Big.TButton", padding=(12, 8))

        self.scanner_info: ScannerInfo | None = None
        self.scanned_images: list[bytes] = []
        self.doc_info: DocumentInfo | None = None
        self.config: Config | None = None

        self._build_ui()
        self._try_load_config()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)

        pad = {"padx": 12, "pady": 4}
        row = 0

        # --- Scanner Connection ---
        scanner_frame = ttk.LabelFrame(self.root, text="Scanner", padding=8)
        scanner_frame.grid(row=row, column=0, padx=12, pady=(12, 4), sticky="ew")
        scanner_frame.columnconfigure(1, weight=1)

        self.scanner_status_label = ttk.Label(
            scanner_frame, text="Not connected", style="Status.TLabel"
        )
        self.scanner_status_label.grid(row=0, column=0, columnspan=3, **pad, sticky="w")

        ttk.Label(scanner_frame, text="IP:").grid(row=1, column=0, padx=(12, 4), pady=4, sticky="w")
        self.scanner_ip_var = tk.StringVar()
        self.scanner_ip_entry = ttk.Entry(scanner_frame, textvariable=self.scanner_ip_var)
        self.scanner_ip_entry.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self.scanner_ip_entry.insert(0, "")

        self.connect_btn = ttk.Button(scanner_frame, text="Connect", command=self._on_connect)
        self.connect_btn.grid(row=1, column=2, padx=(4, 12), pady=4)

        ttk.Label(scanner_frame, text="Leave IP blank for auto-discover", foreground="gray").grid(
            row=2, column=0, columnspan=3, padx=12, pady=(0, 4), sticky="w"
        )

        row += 1

        # --- Scan Settings ---
        settings_frame = ttk.LabelFrame(self.root, text="Settings", padding=8)
        settings_frame.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        settings_frame.columnconfigure(1, weight=1)

        # Source
        ttk.Label(settings_frame, text="Source:").grid(row=0, column=0, padx=(12, 4), pady=4, sticky="w")
        self.source_var = tk.StringVar(value="Feeder")
        source_frame = ttk.Frame(settings_frame)
        source_frame.grid(row=0, column=1, columnspan=3, padx=4, pady=4, sticky="w")
        ttk.Radiobutton(source_frame, text="Document Feeder (ADF)", variable=self.source_var, value="Feeder").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(source_frame, text="Flatbed", variable=self.source_var, value="Platen").pack(side="left")

        # Resolution
        ttk.Label(settings_frame, text="Resolution:").grid(row=1, column=0, padx=(12, 4), pady=4, sticky="w")
        self.resolution_var = tk.StringVar(value="300")
        res_combo = ttk.Combobox(
            settings_frame, textvariable=self.resolution_var,
            values=["150", "200", "300", "600"], state="readonly", width=8
        )
        res_combo.grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(settings_frame, text="DPI").grid(row=1, column=2, padx=0, pady=4, sticky="w")

        # Color mode
        ttk.Label(settings_frame, text="Color:").grid(row=2, column=0, padx=(12, 4), pady=4, sticky="w")
        self.color_var = tk.StringVar(value="Color")
        color_frame = ttk.Frame(settings_frame)
        color_frame.grid(row=2, column=1, columnspan=3, padx=4, pady=4, sticky="w")
        ttk.Radiobutton(color_frame, text="Color", variable=self.color_var, value="Color").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(color_frame, text="Grayscale", variable=self.color_var, value="Grayscale").pack(side="left")

        # Output directory
        ttk.Label(settings_frame, text="Output:").grid(row=3, column=0, padx=(12, 4), pady=4, sticky="w")
        self.output_dir_var = tk.StringVar(value=str(Path("~/Documents/Scans").expanduser()))
        ttk.Entry(settings_frame, textvariable=self.output_dir_var).grid(
            row=3, column=1, columnspan=2, padx=4, pady=4, sticky="ew"
        )
        ttk.Button(settings_frame, text="Browse", command=self._browse_output).grid(
            row=3, column=3, padx=(4, 12), pady=4
        )

        row += 1

        # --- Action Buttons ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.grid(row=row, column=0, padx=12, pady=8, sticky="ew")
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        self.scan_btn = ttk.Button(
            btn_frame, text="Scan & Classify", style="Big.TButton",
            command=self._on_scan_classify, state="disabled"
        )
        self.scan_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.scan_only_btn = ttk.Button(
            btn_frame, text="Scan Only", style="Big.TButton",
            command=self._on_scan_only, state="disabled"
        )
        self.scan_only_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        row += 1

        # --- Progress ---
        self.progress_var = tk.IntVar(value=0)
        self.progress_bar = ttk.Progressbar(self.root, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=row, column=0, padx=12, pady=(0, 4), sticky="ew")

        row += 1

        # --- Results ---
        results_frame = ttk.LabelFrame(self.root, text="Classification Results", padding=8)
        results_frame.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        results_frame.columnconfigure(1, weight=1)

        labels = ["Category:", "Filename:", "Summary:", "Date:"]
        self.result_values: list[ttk.Label] = []
        for i, label_text in enumerate(labels):
            ttk.Label(results_frame, text=label_text, font=("Helvetica", 12, "bold")).grid(
                row=i, column=0, padx=(12, 8), pady=3, sticky="nw"
            )
            val = ttk.Label(results_frame, text="--", wraplength=450, anchor="w", justify="left")
            val.grid(row=i, column=1, padx=(0, 12), pady=3, sticky="w")
            self.result_values.append(val)

        row += 1

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text="Activity Log", padding=8)
        log_frame.grid(row=row, column=0, padx=12, pady=(4, 12), sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.root.rowconfigure(row, weight=1)

        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                                bg="#f5f5f5", font=("Menlo", 11), relief="sunken", bd=1)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    # ── Helpers ──────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_scanner_status(self, text: str, connected: bool) -> None:
        style = "StatusOK.TLabel" if connected else "Status.TLabel"
        self.scanner_status_label.configure(text=text, style=style)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "!disabled"
        self.scan_btn.state([state])
        self.scan_only_btn.state([state])
        self.connect_btn.state([state])
        if busy:
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start(15)
        else:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_var.set(0)

    def _clear_results(self) -> None:
        for val in self.result_values:
            val.configure(text="--")

    def _show_results(self, info: DocumentInfo) -> None:
        texts = [info.category, info.filename, info.summary, info.date or "--"]
        for val, text in zip(self.result_values, texts):
            val.configure(text=text)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if path:
            self.output_dir_var.set(path)

    def _try_load_config(self) -> None:
        try:
            self.config = load_config()
            self._log("Config loaded from .env")
            if self.config.scanner_ip:
                self.scanner_ip_var.set(self.config.scanner_ip)
        except RuntimeError:
            self._log("No ANTHROPIC_API_KEY found. Set it in .env for AI classification.")

    def _get_current_config(self) -> Config:
        """Build a Config from current GUI state."""
        color_map = {"Color": "RGB24", "Grayscale": "Grayscale8"}
        overrides = {
            "scan_source": self.source_var.get(),
            "color_mode": color_map.get(self.color_var.get(), "RGB24"),
            "resolution": int(self.resolution_var.get()),
            "output_dir": self.output_dir_var.get(),
        }
        ip = self.scanner_ip_var.get().strip()
        if ip:
            overrides["scanner_ip"] = ip
        return load_config(**overrides)

    # ── Background task runner ───────────────────────────────────────

    def _run_in_thread(self, target, *args) -> None:
        def wrapper():
            try:
                target(*args)
            except Exception as e:
                self.root.after(0, lambda: self._on_task_error(e))
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        self._set_busy(True)
        threading.Thread(target=wrapper, daemon=True).start()

    def _on_task_error(self, error: Exception) -> None:
        self._log(f"Error: {error}")

    # ── Actions ──────────────────────────────────────────────────────

    def _on_connect(self) -> None:
        self._run_in_thread(self._connect_scanner)

    def _connect_scanner(self) -> None:
        ip = self.scanner_ip_var.get().strip()

        if ip:
            self.root.after(0, lambda: self._log(f"Connecting to {ip}..."))
            info = scanner_info_from_ip(ip)
        else:
            self.root.after(0, lambda: self._log("Searching for Canon scanner..."))
            info = discover_scanner(timeout=8.0)

        # Test the connection
        client = ESCLClient(info.base_url)
        status = client.get_status()
        caps = client.get_capabilities()
        client.close()

        self.scanner_info = info

        def update_ui():
            self._set_scanner_status(f"{info.name}  --  {status.state}", True)
            self._log(f"Connected: {info.name} at {info.ip}")
            self._log(f"  State: {status.state}, ADF: {status.adf_state or 'N/A'}")
            self._log(f"  Sources: {caps.sources}, Resolutions: {caps.resolutions}")
            self.scan_btn.state(["!disabled"])
            self.scan_only_btn.state(["!disabled"])

        self.root.after(0, update_ui)

    def _on_scan_classify(self) -> None:
        self._clear_results()
        self._run_in_thread(self._do_scan, True)

    def _on_scan_only(self) -> None:
        self._clear_results()
        self._run_in_thread(self._do_scan, False)

    def _do_scan(self, classify: bool) -> None:
        config = self._get_current_config()

        # Ensure we have a scanner connection
        if self.scanner_info is None:
            self._connect_scanner()

        with ESCLClient(self.scanner_info.base_url) as client:
            # Check status
            status = client.get_status()
            if status.state != "Idle":
                raise AutoScanError(f"Scanner is {status.state}. Wait and try again.")

            self.root.after(0, lambda: self._log("Scanning..."))

            settings = ScanSettings(
                source=config.scan_source,
                color_mode=config.color_mode,
                resolution=config.resolution,
                document_format=config.scan_format,
            )
            images = client.scan(settings)

        self.scanned_images = images
        self.root.after(0, lambda: self._log(f"Scanned {len(images)} page(s)"))

        if classify:
            self.root.after(0, lambda: self._log("Analyzing with Claude Vision..."))
            self.root.after(0, lambda: self.progress_var.set(50))

            doc_info = analyze_document(images, config)
            self.doc_info = doc_info

            self.root.after(0, lambda: self._show_results(doc_info))
            self.root.after(0, lambda: self._log(f"Classified as: {doc_info.category}"))

            output_path = save_document(images, doc_info, config)
            self.root.after(0, lambda: self._log(f"Saved: {output_path}"))
            self.root.after(0, lambda: self.progress_var.set(100))
        else:
            output_path = save_unclassified(images, config)
            self.root.after(0, lambda: self._log(f"Saved (unclassified): {output_path}"))
            self.root.after(0, lambda: self.progress_var.set(100))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = AutoScanApp()
    app.run()


if __name__ == "__main__":
    main()
