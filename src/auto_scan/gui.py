"""GUI for auto-scan using customtkinter."""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from auto_scan import AutoScanError
from auto_scan.analyzer import DocumentInfo, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.organizer import save_document, save_unclassified
from auto_scan.scanner.discovery import ScannerInfo, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings


class AutoScanApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("Auto-Scan")
        self.geometry("700x780")
        self.minsize(600, 700)

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.scanner_info: ScannerInfo | None = None
        self.escl_client: ESCLClient | None = None
        self.scanned_images: list[bytes] = []
        self.doc_info: DocumentInfo | None = None
        self.config: Config | None = None

        self._build_ui()
        self._try_load_config()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        row = 0

        # --- Scanner Connection ---
        self.scanner_frame = ctk.CTkFrame(self)
        self.scanner_frame.grid(row=row, column=0, padx=12, pady=(12, 6), sticky="ew")
        self.scanner_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.scanner_frame, text="Scanner", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 2), sticky="w"
        )

        self.scanner_status_label = ctk.CTkLabel(
            self.scanner_frame, text="Not connected", text_color="gray"
        )
        self.scanner_status_label.grid(row=0, column=1, padx=12, pady=(10, 2), sticky="w")

        self.scanner_ip_entry = ctk.CTkEntry(
            self.scanner_frame, placeholder_text="Scanner IP (leave blank for auto-discover)"
        )
        self.scanner_ip_entry.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")

        self.connect_btn = ctk.CTkButton(
            self.scanner_frame, text="Connect", command=self._on_connect
        )
        self.connect_btn.grid(row=1, column=2, padx=(4, 12), pady=4)

        row += 1

        # --- Scan Settings ---
        self.settings_frame = ctk.CTkFrame(self)
        self.settings_frame.grid(row=row, column=0, padx=12, pady=6, sticky="ew")
        self.settings_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.settings_frame, text="Settings", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=4, padx=12, pady=(10, 6), sticky="w"
        )

        # Source
        ctk.CTkLabel(self.settings_frame, text="Source:").grid(row=1, column=0, padx=(12, 4), pady=4, sticky="w")
        self.source_var = ctk.StringVar(value="Feeder")
        self.source_menu = ctk.CTkSegmentedButton(
            self.settings_frame, values=["Feeder", "Platen"], variable=self.source_var
        )
        self.source_menu.grid(row=1, column=1, padx=4, pady=4, sticky="w")

        # Resolution
        ctk.CTkLabel(self.settings_frame, text="DPI:").grid(row=1, column=2, padx=(12, 4), pady=4, sticky="w")
        self.resolution_var = ctk.StringVar(value="300")
        self.resolution_menu = ctk.CTkOptionMenu(
            self.settings_frame, values=["150", "200", "300", "600"], variable=self.resolution_var, width=80
        )
        self.resolution_menu.grid(row=1, column=3, padx=(4, 12), pady=4, sticky="w")

        # Color mode
        ctk.CTkLabel(self.settings_frame, text="Color:").grid(row=2, column=0, padx=(12, 4), pady=4, sticky="w")
        self.color_var = ctk.StringVar(value="Color")
        self.color_menu = ctk.CTkSegmentedButton(
            self.settings_frame, values=["Color", "Grayscale"], variable=self.color_var
        )
        self.color_menu.grid(row=2, column=1, padx=4, pady=4, sticky="w")

        # Output directory
        ctk.CTkLabel(self.settings_frame, text="Output:").grid(row=3, column=0, padx=(12, 4), pady=4, sticky="w")
        self.output_dir_var = ctk.StringVar(value=str(Path("~/Documents/Scans").expanduser()))
        self.output_entry = ctk.CTkEntry(self.settings_frame, textvariable=self.output_dir_var)
        self.output_entry.grid(row=3, column=1, columnspan=2, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(self.settings_frame, text="Browse", width=70, command=self._browse_output).grid(
            row=3, column=3, padx=(4, 12), pady=4
        )

        row += 1

        # --- Action Buttons ---
        self.action_frame = ctk.CTkFrame(self)
        self.action_frame.grid(row=row, column=0, padx=12, pady=6, sticky="ew")
        self.action_frame.grid_columnconfigure(0, weight=1)
        self.action_frame.grid_columnconfigure(1, weight=1)

        self.scan_btn = ctk.CTkButton(
            self.action_frame,
            text="Scan & Classify",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=44,
            command=self._on_scan_classify,
            state="disabled",
        )
        self.scan_btn.grid(row=0, column=0, padx=12, pady=12, sticky="ew")

        self.scan_only_btn = ctk.CTkButton(
            self.action_frame,
            text="Scan Only",
            height=44,
            fg_color="gray",
            command=self._on_scan_only,
            state="disabled",
        )
        self.scan_only_btn.grid(row=0, column=1, padx=(0, 12), pady=12, sticky="ew")

        row += 1

        # --- Progress ---
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.grid(row=row, column=0, padx=12, pady=(0, 6), sticky="ew")
        self.progress_bar.set(0)

        row += 1

        # --- Results ---
        self.results_frame = ctk.CTkFrame(self)
        self.results_frame.grid(row=row, column=0, padx=12, pady=6, sticky="ew")
        self.results_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.results_frame, text="Results", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 6), sticky="w"
        )

        labels = ["Category:", "Filename:", "Summary:", "Date:"]
        self.result_values: list[ctk.CTkLabel] = []
        for i, label_text in enumerate(labels):
            ctk.CTkLabel(self.results_frame, text=label_text).grid(
                row=i + 1, column=0, padx=(12, 4), pady=2, sticky="nw"
            )
            val = ctk.CTkLabel(self.results_frame, text="—", wraplength=450, anchor="w", justify="left")
            val.grid(row=i + 1, column=1, padx=(4, 12), pady=2, sticky="w")
            self.result_values.append(val)

        row += 1

        # --- Log ---
        self.log_frame = ctk.CTkFrame(self)
        self.log_frame.grid(row=row, column=0, padx=12, pady=(6, 12), sticky="nsew")
        self.log_frame.grid_columnconfigure(0, weight=1)
        self.log_frame.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(row, weight=1)

        ctk.CTkLabel(self.log_frame, text="Log", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w"
        )

        self.log_text = ctk.CTkTextbox(self.log_frame, height=140, state="disabled")
        self.log_text.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")

    # ── Helpers ──────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_scanner_status(self, text: str, color: str) -> None:
        self.scanner_status_label.configure(text=text, text_color=color)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        self.scan_only_btn.configure(state=state)
        self.connect_btn.configure(state=state)
        if busy:
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(0)

    def _clear_results(self) -> None:
        for val in self.result_values:
            val.configure(text="—")

    def _show_results(self, info: DocumentInfo) -> None:
        texts = [info.category, info.filename, info.summary, info.date or "—"]
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
                self.scanner_ip_entry.insert(0, self.config.scanner_ip)
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
        ip = self.scanner_ip_entry.get().strip()
        if ip:
            overrides["scanner_ip"] = ip
        return load_config(**overrides)

    # ── Background task runner ───────────────────────────────────────

    def _run_in_thread(self, target, *args) -> None:
        def wrapper():
            try:
                target(*args)
            except Exception as e:
                self.after(0, lambda: self._on_task_error(e))
            finally:
                self.after(0, lambda: self._set_busy(False))

        self._set_busy(True)
        threading.Thread(target=wrapper, daemon=True).start()

    def _on_task_error(self, error: Exception) -> None:
        self._log(f"Error: {error}")

    # ── Actions ──────────────────────────────────────────────────────

    def _on_connect(self) -> None:
        self._run_in_thread(self._connect_scanner)

    def _connect_scanner(self) -> None:
        ip = self.scanner_ip_entry.get().strip()

        if ip:
            self.after(0, lambda: self._log(f"Connecting to {ip}..."))
            info = scanner_info_from_ip(ip)
        else:
            self.after(0, lambda: self._log("Searching for Canon scanner..."))
            info = discover_scanner(timeout=8.0)

        # Test the connection
        client = ESCLClient(info.base_url)
        status = client.get_status()
        caps = client.get_capabilities()

        self.scanner_info = info
        self.escl_client = client

        def update_ui():
            self._set_scanner_status(f"{info.name}  ●  {status.state}", "green")
            self._log(f"Connected: {info.name} at {info.ip}")
            self._log(f"  State: {status.state}, ADF: {status.adf_state or 'N/A'}")
            self._log(f"  Sources: {caps.sources}, Resolutions: {caps.resolutions}")
            self.scan_btn.configure(state="normal")
            self.scan_only_btn.configure(state="normal")

        self.after(0, update_ui)

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

        client = ESCLClient(self.scanner_info.base_url)

        # Check status
        status = client.get_status()
        if status.state != "Idle":
            raise AutoScanError(f"Scanner is {status.state}. Wait and try again.")

        self.after(0, lambda: self._log("Scanning..."))

        settings = ScanSettings(
            source=config.scan_source,
            color_mode=config.color_mode,
            resolution=config.resolution,
            document_format=config.scan_format,
        )
        images = client.scan(settings)
        self.scanned_images = images

        self.after(0, lambda: self._log(f"Scanned {len(images)} page(s)"))

        if classify:
            self.after(0, lambda: self._log("Analyzing with Claude Vision..."))
            self.after(0, lambda: self.progress_bar.set(0.5))

            doc_info = analyze_document(images, config)
            self.doc_info = doc_info

            self.after(0, lambda: self._show_results(doc_info))
            self.after(0, lambda: self._log(f"Classified as: {doc_info.category}"))

            output_path = save_document(images, doc_info, config)
            self.after(0, lambda: self._log(f"Saved: {output_path}"))
            self.after(0, lambda: self.progress_bar.set(1.0))
        else:
            output_path = save_unclassified(images, config)
            self.after(0, lambda: self._log(f"Saved (unclassified): {output_path}"))
            self.after(0, lambda: self.progress_bar.set(1.0))


def main() -> None:
    app = AutoScanApp()
    app.mainloop()


if __name__ == "__main__":
    main()
