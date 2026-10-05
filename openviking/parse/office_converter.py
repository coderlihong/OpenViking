# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Office (Word/Excel/PowerPoint) to PDF conversion for the unified parse pipeline.

This module converts ``.doc/.docx/.xls/.xlsx/.ppt/.pptx`` sources to PDF with an
isolated, pinned LibreOffice headless invocation so the existing PDF parser can
be reused unchanged (KB-012 / F8).

Design notes
------------
* AnyDoc -> Markdown is deliberately **not** a completion path here. Office
  sources either convert to PDF and re-enter the PDF parser, or fail with a
  clear, actionable conversion error. There is no silent fallback.
* The LibreOffice invocation is isolated behind :meth:`OfficeConverter._run_libreoffice`
  so unit tests can exercise routing/error/TTL logic without LibreOffice
  installed; the real binary is only needed by the ``integration`` tests.
* Every conversion runs in its own throw-away user profile directory with
  macros disabled, a wall-clock timeout and best-effort OS resource limits.
* Intermediate PDFs live under a converter-owned temp root and are removed by
  :meth:`OfficeConverter.cleanup` (right after the parse materializes its tree)
  and by :meth:`OfficeConverter.cleanup_expired` (TTL sweep).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from threading import Event, local
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from openviking_cli.exceptions import OpenVikingError
logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # Windows requires a separate container worker.
    fcntl = None

__all__ = [
    "OFFICE_EXTENSIONS",
    "OfficeConversionError",
    "OfficeConversionResult",
    "OfficeConverter",
    "OfficeConverterConfig",
]

#: The six Office extensions in the support matrix, mapped to a document kind.
OFFICE_EXTENSIONS: Dict[str, str] = {
    ".doc": "word",
    ".docx": "word",
    ".xls": "excel",
    ".xlsx": "excel",
    ".ppt": "presentation",
    ".pptx": "presentation",
}

#: OOXML (zip container) extensions. Legacy OLE/CFB formats are not listed.
_OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}

#: OLE/CFB magic used to spot encrypted OOXML containers and legacy Office files.
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Marker names that indicate an encrypted legacy OLE Office document.
_LEGACY_ENCRYPTION_MARKERS = (b"EncryptedPackage", b"EncryptionInfo")

#: Marker names that indicate embedded macros.
_MACRO_MARKERS = (b"_VBA_PROJECT", b"vbaProject")

#: Export filter options per document kind.
#: * Word: export heading bookmarks so the PDF parser can align chapters.
#: * Excel: forbid "shrink to one page" (``SinglePageSheets=false``) so long
#:   sheets paginate readably; export bookmarks for sheet provenance.
#: * Presentation: export slide bookmarks (one per slide) to preserve order.
_EXPORT_FILTERS: Dict[str, str] = {
    "word": (
        'pdf:writer_pdf_Export:{"ExportBookmarks":{"type":"boolean","value":"true"},'
        '"ExportNotes":{"type":"boolean","value":"true"}}'
    ),
    "excel": (
        'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"false"},'
        '"ExportBookmarks":{"type":"boolean","value":"true"}}'
    ),
    "presentation": ('pdf:impress_pdf_Export:{"ExportBookmarks":{"type":"boolean","value":"true"}}'),
}

#: Common macOS install location, tried after ``shutil.which``.
_MACOS_BINARY = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

#: Best-effort extraction of declared font family names from OOXML parts.
_FONT_ATTR_RE = re.compile(r'(?:typeface|w:ascii|w:hAnsi|w:eastAsia|w:cs)="([^"]+)"')


class OfficeConversionError(OpenVikingError):
    """A clear, actionable Office -> PDF conversion failure.

    ``reason`` is a stable machine-readable discriminator so callers and tests
    can distinguish missing fonts, password protection, corruption, timeout and
    macro-blocked documents instead of treating every failure as generic.
    """

    def __init__(self, message: str, reason: str, *, source: Optional[str] = None, **extra: Any):
        details: Dict[str, Any] = {"reason": reason}
        if source:
            details["source"] = str(source)
        details.update(extra)
        super().__init__(message, code="OFFICE_CONVERSION_FAILED", details=details)
        self.reason = reason


@dataclass
class OfficeConverterConfig:
    """Pinned, resource-bounded LibreOffice conversion settings."""

    #: Explicit binary path. When empty the converter searches ``PATH`` and the
    #: standard macOS application bundle.
    binary: Optional[str] = None
    #: Wall-clock timeout for a single conversion, in seconds.
    timeout: float = 180.0
    #: TTL for intermediate PDF/profile artifacts, in seconds.
    ttl_seconds: float = 3600.0
    #: Reject inputs larger than this many bytes before invoking LibreOffice.
    max_input_bytes: int = 512 * 1024 * 1024
    #: Root directory for throw-away conversion work dirs (defaults to tmpdir).
    temp_root: Optional[Path] = None
    #: Fonts the deployment guarantees are present. They are treated as
    #: installed-equivalent during the missing-font check.
    required_fonts: Tuple[str, ...] = ()
    #: Whether to fail loudly when a source declares an unavailable font.
    check_fonts: bool = True
    #: Best-effort OS resource limits applied to the child process (POSIX only).
    cpu_seconds: int = 300
    address_space_bytes: int = 4 * 1024**3
    file_size_bytes: int = 2 * 1024**3
    #: Remove the original Office source once conversion succeeds.
    delete_source_after_parse: bool = False
    #: Recorded for provenance; macro execution is always disabled.
    macro_execution: str = "disabled"
    max_container_bytes: int = 2 * 1024**3
    max_container_members: int = 20000
    expected_version: Optional[str] = None
    worker_threads: int = 2
    font_inventory_from_binary: bool = False

    @classmethod
    def from_environment(cls) -> "OfficeConverterConfig":
        """Explicit deployment overrides shared by router and job preflight.

        Remote transports implement the soffice command protocol and the optional
        ``--font-inventory`` probe. No credentials are stored in this configuration.
        """
        values = {}
        binary = os.environ.get("OPENVIKING_OFFICE_BINARY")
        if binary:
            path = Path(binary)
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("OPENVIKING_OFFICE_BINARY must name an executable absolute path")
            values["binary"] = str(path)
        version = os.environ.get("OPENVIKING_OFFICE_EXPECTED_VERSION")
        if version:
            values["expected_version"] = version
        for variable, field_name in (("OPENVIKING_OFFICE_CHECK_FONTS", "check_fonts"),
                                     ("OPENVIKING_OFFICE_FONT_INVENTORY_FROM_BINARY", "font_inventory_from_binary")):
            value = os.environ.get(variable)
            if value is not None:
                if value.lower() not in {"true", "false"}:
                    raise ValueError(f"{variable} must be true or false")
                values[field_name] = value.lower() == "true"
        return cls(**values)

    def validate(self) -> None:
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Office conversion timeout must be positive")
        if isinstance(self.ttl_seconds, bool) or not isinstance(self.ttl_seconds, (int, float)) or not math.isfinite(self.ttl_seconds) or self.ttl_seconds <= 0:
            raise ValueError("Office conversion TTL must be positive")
        if self.max_input_bytes <= 0:
            raise ValueError("Office conversion max_input_bytes must be positive")
        if self.macro_execution != "disabled":
            raise ValueError("Office conversion must run with macros disabled")
        for field_name in ("max_input_bytes", "max_container_bytes", "max_container_members", "cpu_seconds", "address_space_bytes", "file_size_bytes", "worker_threads"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Office conversion {field_name} must be a positive integer")


@dataclass
class OfficeConversionResult:
    """Outcome of a successful Office -> PDF conversion."""

    pdf_path: Path
    kind: str
    extension: str
    source_path: Path
    work_dir: Path
    profile_dir: Path
    created_at: float
    ttl_seconds: float
    binary: str = ""
    structure: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    source_sha256: str = ""
    pdf_sha256: str = ""
    converter_version: str = ""
    lease: Any = field(default=None, repr=False)

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def metadata(self) -> Dict[str, Any]:
        """Provenance metadata to merge into the parse result ``meta``."""
        meta: Dict[str, Any] = {
            "office_source_extension": self.extension,
            "office_document_kind": self.kind,
            "office_converter": "libreoffice-headless",
            "office_converter_binary": self.binary,
            "office_macro_execution": "disabled",
            "office_intermediate_pdf": str(self.pdf_path),
            "office_intermediate_pdf_ttl_seconds": self.ttl_seconds,
            "office_structure_source": "libreoffice_pdf_bookmarks",
            "office_source_sha256": self.source_sha256,
            "office_pdf_sha256": self.pdf_sha256,
            "office_converter_version": self.converter_version,
            "office_source_cleanup_owner": "ingestion_lifecycle",
        }
        if self.structure:
            meta["office_structure"] = self.structure
        return meta


class OfficeConverter:
    """Convert Office documents to PDF via isolated LibreOffice headless runs."""

    def __init__(self, config: Optional[OfficeConverterConfig] = None):
        self.config = config if config is not None else OfficeConverterConfig.from_environment()
        self.config.validate()
        root = self.config.temp_root or (Path(tempfile.gettempdir()) / "openviking_office")
        self._temp_root = Path(root)
        self._installed_fonts_cache: Optional[Set[str]] = None
        self._version_cache: Optional[str] = None
        self._thread_state = local()

    # ------------------------------------------------------------------ routing
    @staticmethod
    def _normalize_extension(extension: str) -> str:
        ext = str(extension or "").lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        return ext

    def kind_for_extension(self, extension: str) -> Optional[str]:
        """Return the document kind for an Office extension, or ``None``."""
        return OFFICE_EXTENSIONS.get(self._normalize_extension(extension))

    def is_office_extension(self, extension: str) -> bool:
        """Return whether ``extension`` is one of the six supported Office types."""
        return self.kind_for_extension(extension) is not None

    @property
    def supported_extensions(self) -> List[str]:
        return list(OFFICE_EXTENSIONS)

    @property
    def temp_root(self) -> Path:
        return self._temp_root

    # ------------------------------------------------------------ binary lookup
    def _locate_binary(self) -> str:
        if self.config.binary:
            candidate = Path(self.config.binary)
            if candidate.is_file():
                return str(candidate)
            found = shutil.which(self.config.binary)
            if found:
                return found
            raise OfficeConversionError(
                f"Configured LibreOffice binary not found: {self.config.binary}",
                "converter_unavailable",
            )
        for name in ("soffice", "libreoffice"):
            found = shutil.which(name)
            if found:
                return found
        if Path(_MACOS_BINARY).is_file():
            return _MACOS_BINARY
        raise OfficeConversionError(
            "LibreOffice (soffice/libreoffice) is not installed; Office sources "
            "cannot be converted to PDF and will not silently fall back to AnyDoc",
            "converter_unavailable",
        )

    def is_available(self) -> bool:
        """Whether a LibreOffice binary can be located."""
        try:
            self._locate_binary()
            if self.config.expected_version:
                self.version()
        except OfficeConversionError:
            return False
        return True

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(part)
        return digest.hexdigest()

    def version(self) -> str:
        if self._version_cache is None:
            try:
                probe = subprocess.run([self._locate_binary(), "--version"], capture_output=True, text=True, timeout=15)
                output = (probe.stdout or "").strip()
                self._version_cache = output if probe.returncode == 0 and output.startswith("LibreOffice") else "unknown"
            except (OSError, subprocess.SubprocessError):
                self._version_cache = "unknown"
        if self.config.expected_version and self._version_cache != self.config.expected_version:
            raise OfficeConversionError("Office converter version does not match the frozen deployment", "version_mismatch")
        return self._version_cache

    # --------------------------------------------------------- input inspection
    @staticmethod
    def _read_head(path: Path, size: int = 8) -> bytes:
        try:
            with path.open("rb") as handle:
                return handle.read(size)
        except OSError:
            return b""

    def _assert_not_password_protected(self, path: Path, ext: str) -> None:
        """Detect encrypted Office documents before wasting a conversion run."""
        if ext in _OOXML_EXTENSIONS:
            # Encrypted OOXML is wrapped in an OLE/CFB container instead of a zip.
            if self._read_head(path) == _CFB_MAGIC:
                raise OfficeConversionError(
                    "Password-protected Office document is not supported",
                    "password_protected",
                    source=path,
                )
            return
        # Legacy OLE formats expose EncryptionInfo/EncryptedPackage streams.
        try:
            with path.open("rb") as handle:
                blob = handle.read(4 * 1024 * 1024)
        except OSError:
            return
        if any(marker in blob for marker in _LEGACY_ENCRYPTION_MARKERS):
            raise OfficeConversionError(
                "Password-protected Office document is not supported",
                "password_protected",
                source=path,
            )

    def _assert_not_corrupt(self, path: Path, ext: str) -> None:
        """Reject structurally invalid inputs with a clear error."""
        if ext in _OOXML_EXTENSIONS:
            try:
                with zipfile.ZipFile(path) as archive:
                    entries = archive.infolist()
                    if len(entries) > self.config.max_container_members or sum(entry.file_size for entry in entries) > self.config.max_container_bytes:
                        raise OfficeConversionError("Office container exceeds configured extraction limits", "resource_limit", source=path)
                    names = set(archive.namelist())
                    if len(names) != len(entries) or any(entry.flag_bits & 1 for entry in entries):
                        raise OfficeConversionError("Duplicate or encrypted Office container members", "corrupt", source=path)
            except zipfile.BadZipFile as exc:
                raise OfficeConversionError(
                    f"Corrupt Office document (invalid OOXML container): {exc}",
                    "corrupt",
                    source=path,
                ) from exc
            if "[Content_Types].xml" not in names:
                raise OfficeConversionError(
                    "Corrupt Office document (missing [Content_Types].xml)",
                    "corrupt",
                    source=path,
                )
            return
        if self._read_head(path, len(_CFB_MAGIC)) != _CFB_MAGIC:
            raise OfficeConversionError(
                "Corrupt Office document (unrecognized legacy container)",
                "corrupt",
                source=path,
            )

    def _assert_no_macros(self, path: Path, ext: str) -> None:
        """Refuse macro documents; macro execution is never enabled."""
        if ext in _OOXML_EXTENSIONS:
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
            except zipfile.BadZipFile:
                return
            if any(name.endswith("vbaProject.bin") for name in names):
                raise OfficeConversionError(
                    "Macro-enabled Office document is not supported (macros disabled)",
                    "macro_blocked",
                    source=path,
                )
            return
        try:
            with path.open("rb") as handle:
                blob = handle.read(4 * 1024 * 1024)
        except OSError:
            return
        if any(marker in blob for marker in _MACRO_MARKERS):
            raise OfficeConversionError(
                "Macro-enabled Office document is not supported (macros disabled)",
                "macro_blocked",
                source=path,
            )

    @staticmethod
    def _declared_fonts(path: Path, ext: str) -> Set[str]:
        """Best-effort set of font families declared by an OOXML source."""
        if ext not in _OOXML_EXTENSIONS:
            return set()
        fonts: Set[str] = set()
        from defusedxml import ElementTree as safe_xml

        def add_font(value: str) -> None:
            value = value.strip()
            # DrawingML +mj/+mn names are theme references, not font families.
            # The referenced theme's major/minor declarations are inspected too.
            if value and not value.startswith(("{", "+mj-", "+mn-")) and len(value) < 128:
                fonts.add(value)

        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.endswith(".xml"):
                        continue
                    if not name.startswith(("word/", "xl/", "ppt/", "docProps/")):
                        continue
                    try:
                        text = archive.read(name).decode("utf-8", errors="ignore")
                    except (KeyError, OSError):
                        continue
                    try:
                        root = safe_xml.fromstring(text)
                    except (safe_xml.ParseError, ValueError):
                        root = None
                    if root is not None:
                        for element in root.iter():
                            tag = element.tag.rsplit("}", 1)[-1]
                            if tag not in {"rFonts", "font", "latin", "ea", "cs"}:
                                continue
                            attributes = {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
                            for key in ("typeface", "ascii", "hAnsi", "eastAsia", "cs"):
                                if key in attributes:
                                    add_font(attributes[key])
                            if tag == "font":
                                # Word's font table stores w:name; SpreadsheetML
                                # instead stores <font><name val="..."/></font>.
                                if "name" in attributes:
                                    add_font(attributes["name"])
                                if name.startswith("xl/"):
                                    for child in element:
                                        if child.tag.rsplit("}", 1)[-1] == "name":
                                            add_font(child.attrib.get("val", ""))
                        continue
                    # w:cs also occurs on w:lang and contains a locale there.
                    # Inspect font-bearing tags rather than rejecting language
                    # names such as en-US as supposedly missing fonts.
                    font_tags = re.findall(r'<(?:[\w.-]+:)?(?:rFonts|font|latin|ea|cs)\b[^>]*>', text)
                    for value in _FONT_ATTR_RE.findall(" ".join(font_tags)):
                        add_font(value)
        except zipfile.BadZipFile:
            return set()
        return fonts

    def _installed_fonts(self) -> Set[str]:
        """Installed font families via ``fc-list``; empty when undeterminable."""
        if self._installed_fonts_cache is not None:
            return self._installed_fonts_cache
        fonts: Set[str] = set()
        fc_list = self._locate_binary() if self.config.font_inventory_from_binary else shutil.which("fc-list")
        if fc_list:
            try:
                completed = subprocess.run(
                    ([fc_list, "--font-inventory"] if self.config.font_inventory_from_binary
                     else [fc_list, "--format", "%{family}\n"]),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                for line in (completed.stdout.splitlines() if completed.returncode == 0 else []):
                    for family in line.split(","):
                        family = family.strip()
                        if family:
                            fonts.add(family)
            except (OSError, subprocess.SubprocessError):
                fonts = set()
        if self.config.font_inventory_from_binary and not fonts:
            raise OfficeConversionError("Remote font inventory could not be verified", "converter_unavailable")
        self._installed_fonts_cache = fonts
        return fonts

    def _assert_fonts_available(self, path: Path, ext: str) -> None:
        """Fail loudly when a source declares a font the deployment lacks."""
        if not self.config.check_fonts:
            return
        declared = self._declared_fonts(path, ext)
        if not declared:
            return
        installed = self._installed_fonts()
        if not installed:
            # Font inventory unavailable (e.g. no fontconfig); cannot judge.
            return
        guaranteed = set(self.config.required_fonts)
        missing = sorted(f for f in declared if f not in installed and f not in guaranteed)
        if missing:
            raise OfficeConversionError(
                "Office document requires fonts that are not installed: " + ", ".join(missing),
                "missing_fonts",
                source=path,
                missing_fonts=missing,
            )

    # ----------------------------------------------------------- LibreOffice run
    def _limited_command(self, argv: List[str]) -> List[str]:
        """Apply limits in a fresh interpreter, never a threaded fork callback."""
        if os.name != "posix":
            raise OfficeConversionError(
                "Office conversion requires a POSIX worker with resource limits",
                "converter_unavailable",
            )
        wrapper = """import os, resource, sys
cpu, address, size = map(int, sys.argv[1:4])
limits = [(resource.RLIMIT_CPU, cpu), (resource.RLIMIT_FSIZE, size)]
if sys.platform != "darwin":
    limits.append((resource.RLIMIT_AS, address))
for limit, value in limits:
    _, hard = resource.getrlimit(limit)
    effective = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(limit, (effective, effective))
os.execv(sys.argv[4], sys.argv[4:])
"""
        return [sys.executable, "-c", wrapper, str(self.config.cpu_seconds),
                str(self.config.address_space_bytes), str(self.config.file_size_bytes), *argv]

    def _run_libreoffice(
        self,
        source: Path,
        work_dir: Path,
        profile_dir: Path,
        export_filter: str,
        timeout: float,
    ) -> subprocess.CompletedProcess:
        """Run LibreOffice headless once. Isolated seam so tests can stub it.

        Raises:
            OfficeConversionError: on timeout (``reason="timeout"``).
        """
        binary = self._locate_binary()
        profile_dir.mkdir(parents=True, exist_ok=True)
        user = profile_dir / "user"
        user.mkdir(exist_ok=True)
        # These are real LibreOffice policy properties, not merely provenance labels.
        (user / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><oor:items '
            'xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>'
        )
        argv = [
            binary,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--nologo",
            # Isolated user profile: fixed version/fonts, no shared state.
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            export_filter,
            "--outdir",
            str(work_dir),
            str(source),
        ]
        try:
            environment = os.environ.copy()
            environment.update(
                HOME=str(profile_dir), XDG_CACHE_HOME=str(profile_dir / "cache"),
                MAX_CONCURRENCY=str(self.config.worker_threads),
                OMP_NUM_THREADS=str(self.config.worker_threads), MALLOC_ARENA_MAX="2",
            )
            process = subprocess.Popen(
                self._limited_command(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=environment,
            )
            deadline = time.monotonic() + timeout
            try:
                while True:
                    cancelled = getattr(self._thread_state, "cancel_event", None)
                    if cancelled is not None and cancelled.is_set():
                        raise OfficeConversionError("Office conversion cancelled", "cancelled", source=source)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OfficeConversionError(f"Office to PDF conversion timed out after {timeout:g}s", "timeout", source=source)
                    try:
                        stdout, stderr = process.communicate(timeout=min(.2, remaining))
                        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                self._stop_process_group(process)
                raise
        except FileNotFoundError as exc:
            raise OfficeConversionError(
                f"LibreOffice binary could not be executed: {binary}",
                "converter_unavailable",
                source=source,
            ) from exc
        except OSError as exc:
            raise OfficeConversionError(f"Failed to launch LibreOffice: {exc}", "conversion_failed", source=source) from exc

    @staticmethod
    def _stop_process_group(process) -> None:
        """Stop the launcher and its LibreOffice descendants before deleting files."""
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.communicate(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.communicate(timeout=2)

    def _locate_output_pdf(self, work_dir: Path, source: Path) -> Path:
        """Find the converted PDF, failing clearly when LibreOffice produced none."""
        expected = work_dir / f"{source.stem}.pdf"
        if expected.is_file():
            return expected
        candidates = sorted(work_dir.glob("*.pdf"))
        if len(candidates) == 1:
            return candidates[0]
        raise OfficeConversionError(
            "LibreOffice did not produce a PDF output",
            "conversion_failed",
            source=source,
        )

    @staticmethod
    def _read_pdf_structure(pdf_path: Path) -> Dict[str, Any]:
        """Read bookmarks/page count from the converted PDF for provenance.

        Excel sheets and PowerPoint slides both surface as PDF bookmarks, so the
        existing PDF parser can locate sheet/slide provenance. When bookmarks are
        absent the PDF parser falls back to Markdown headings/rules.
        """
        try:
            import pymupdf
        except Exception:  # pragma: no cover - pymupdf is a hard runtime dep
            raise OfficeConversionError("PDF validation runtime unavailable", "converter_unavailable", source=pdf_path)
        try:
            with pymupdf.open(str(pdf_path)) as document:
                if document.needs_pass:
                    raise OfficeConversionError(
                        "Converted PDF is password-protected",
                        "password_protected",
                        source=pdf_path,
                    )
                page_count = len(document)
                if page_count < 1:
                    raise OfficeConversionError("Converted PDF has no pages", "conversion_failed", source=pdf_path)
                bookmarks = [
                    {"level": level, "title": title.strip(), "page_num": page}
                    for level, title, page in document.get_toc()
                    if title and title.strip()
                ]
        except OfficeConversionError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise OfficeConversionError("Converted output is not a readable PDF", "conversion_failed", source=pdf_path) from exc
        return {
            "page_count": page_count,
            "bookmark_count": len(bookmarks),
            "bookmarks": bookmarks,
        }

    # ---------------------------------------------------------------- conversion
    def convert_sync(
        self,
        source: Path,
        *,
        resource_name: Optional[str] = None,
        resolved_extension: Optional[str] = None,
        _cancel_event: Optional[Event] = None,
    ) -> OfficeConversionResult:
        """Convert an Office file to PDF synchronously.

        Raises:
            FileNotFoundError: when the source is missing.
            OfficeConversionError: with a stable ``reason`` for every failure.
        """
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"Office document not found: {path}")

        ext = self._normalize_extension(resolved_extension or path.suffix)
        kind = self.kind_for_extension(ext)
        if kind is None:
            raise OfficeConversionError(
                f"Unsupported Office extension: {ext or '<none>'}",
                "unsupported_extension",
                source=path,
            )

        size = path.stat().st_size
        if size <= 0:
            raise OfficeConversionError(
                "Office document is empty", "corrupt", source=path
            )
        if size > self.config.max_input_bytes:
            raise OfficeConversionError(
                f"Office document exceeds the {self.config.max_input_bytes} byte limit",
                "resource_limit",
                source=path,
                size=size,
            )

        # Inspect the container before spending a LibreOffice run.
        self._assert_not_password_protected(path, ext)
        self._assert_not_corrupt(path, ext)
        self._assert_no_macros(path, ext)
        self._assert_fonts_available(path, ext)

        self._temp_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f"office_{kind}_", dir=str(self._temp_root)))
        profile_dir = work_dir / "profile"
        lease = (work_dir / ".active.lock").open("a")
        if fcntl is not None:
            fcntl.flock(lease, fcntl.LOCK_EX)
        self._thread_state.cancel_event = _cancel_event
        try:
            staged = work_dir / ("input" + ext)
            shutil.copyfile(path, staged)
            source_sha256 = self._file_sha256(staged)
            converter_version = self.version()
            sheet_manifest = None
            if kind == "excel":
                from openviking.parse.office_layout import prepare_workbook

                if ext == ".xls":
                    converted = self._run_libreoffice(
                        staged, work_dir, profile_dir, "xlsx:Calc MS Excel 2007 XML",
                        self.config.timeout,
                    )
                    normalized = work_dir / "input.xlsx"
                    if converted.returncode != 0 or not normalized.is_file():
                        raise OfficeConversionError(
                            "Legacy Excel normalization did not produce OOXML: " + (converted.stderr or "").strip(),
                            "conversion_failed", source=path, returncode=converted.returncode,
                        )
                else:
                    normalized = staged
                prepared = work_dir / "printable.xlsx"
                try:
                    sheet_manifest = prepare_workbook(normalized, prepared)
                except (ValueError, KeyError, zipfile.BadZipFile) as exc:
                    raise OfficeConversionError(
                        "Cannot establish the Excel used-sheet print policy",
                        "corrupt", source=path,
                    ) from exc
                staged = prepared
            completed = self._run_libreoffice(
                staged,
                work_dir,
                profile_dir,
                _EXPORT_FILTERS[kind],
                self.config.timeout,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                raise OfficeConversionError(
                    "LibreOffice failed to convert the Office document"
                    + (f": {stderr or stdout}" if (stderr or stdout) else ""),
                    "conversion_failed",
                    source=path,
                    returncode=completed.returncode,
                )
            pdf_path = self._locate_output_pdf(work_dir, staged)
            if pdf_path.is_symlink() or not pdf_path.resolve().is_relative_to(work_dir.resolve()) or pdf_path.stat().st_size > self.config.file_size_bytes:
                raise OfficeConversionError("Unsafe or oversized intermediate PDF", "resource_limit", source=path)
            structure = self._read_pdf_structure(pdf_path)
            if sheet_manifest is not None:
                structure["sheets"] = sheet_manifest
                structure["print_policy"] = "all_used_sheets_ignore_source_print_area"
        except BaseException:
            # Never leak an intermediate work dir on failure.
            shutil.rmtree(work_dir, ignore_errors=True)
            lease.close()
            raise

        binary = ""
        try:
            binary = self._locate_binary()
        except OfficeConversionError:
            binary = ""

        warnings: List[str] = []
        if sys.platform == "darwin":
            warnings.append("macOS converter enforces CPU/file limits; memory containment requires the pinned Linux container")
        if kind == "excel":
            # Formula execution semantics are explicitly out of scope.
            warnings.append(
                "Excel is ingested as rendered PDF content; formula calculation "
                "semantics are not guaranteed."
            )
        if not structure.get("bookmark_count"):
            warnings.append(
                "Converted PDF has no bookmarks; structure falls back to "
                "Markdown headings/rules in the PDF parser."
            )

        return OfficeConversionResult(
            pdf_path=pdf_path,
            kind=kind,
            extension=ext,
            source_path=path,
            work_dir=work_dir,
            profile_dir=profile_dir,
            created_at=time.time(),
            ttl_seconds=self.config.ttl_seconds,
            binary=binary,
            structure=structure,
            warnings=warnings,
            source_sha256=source_sha256,
            pdf_sha256=self._file_sha256(pdf_path),
            converter_version=converter_version,
            lease=lease,
        )

    async def convert(
        self,
        source: Path,
        *,
        resource_name: Optional[str] = None,
        resolved_extension: Optional[str] = None,
    ) -> OfficeConversionResult:
        """Async wrapper that offloads the blocking conversion to a thread."""
        cancel_event = Event()
        task = asyncio.create_task(asyncio.to_thread(self.convert_sync, Path(source), resource_name=resource_name,
                                                   resolved_extension=resolved_extension, _cancel_event=cancel_event))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                result = await asyncio.shield(task)
                self.cleanup(result)
            except (OfficeConversionError, asyncio.CancelledError):
                if not task.done():
                    def cleanup_after_cancel(finished):
                        if not finished.cancelled() and finished.exception() is None:
                            self.cleanup(finished.result())
                    task.add_done_callback(cleanup_after_cancel)
            raise

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, result: OfficeConversionResult) -> None:
        """Remove the intermediate work dir (PDF + profile) for one conversion."""
        if not result.work_dir.resolve().is_relative_to(self._temp_root.resolve()) or result.work_dir.is_symlink():
            raise OfficeConversionError("Refusing cleanup outside converter-owned work root", "ownership_mismatch")
        if result.lease is not None:
            result.lease.close()
        shutil.rmtree(result.work_dir, ignore_errors=True)

    def cleanup_expired(self, now: Optional[float] = None) -> List[Path]:
        """Remove work dirs older than the TTL. Returns removed paths."""
        if not self._temp_root.exists():
            return []
        reference = now if now is not None else time.time()
        removed: List[Path] = []
        for child in self._temp_root.iterdir():
            if not child.is_dir() or child.is_symlink() or not child.name.startswith("office_"):
                continue
            try:
                age = reference - child.stat().st_mtime
            except OSError:
                continue
            if age >= self.config.ttl_seconds:
                lease_path = child / ".active.lock"
                lock = None
                if lease_path.exists():
                    if fcntl is None or lease_path.is_symlink():
                        continue
                    lock = lease_path.open("a")
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock.close()
                        continue
                try:
                    shutil.rmtree(child, ignore_errors=True)
                finally:
                    if lock is not None:
                        lock.close()
                removed.append(child)
                logger.info("Cleaned expired Office conversion artifact: %s", child)
        return removed

    def cleanup_source(self, result: OfficeConversionResult) -> None:
        """Optionally remove the original Office source after a successful parse.

        Only runs when ``delete_source_after_parse`` is enabled; persisted parse
        results never re-read the original, so this is safe once the tree exists.
        """
        # A converter does not own user originals. Temporary upload/download
        # cleanup belongs to the ingestion lifecycle, including failed parses.
        if self.config.delete_source_after_parse:
            logger.warning("Office original retained: source cleanup belongs to ingestion lifecycle")
