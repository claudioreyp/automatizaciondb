"""Validation for an original menu document, never an agent input or image."""
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

from fastapi import HTTPException
from pypdf import PdfReader, apply_configuration

MENU_PDF_MAX_BYTES = 8 * 1024 * 1024
MENU_PDF_PARSE_TIMEOUT_SECONDS = 5


def menu_pdf_filename(filename: str | None) -> str:
    name = unicodedata.normalize("NFC", filename or "carta.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    stem = (name[:-4] if name.lower().endswith(".pdf") else name).strip(" .")[:160]
    # Leave room for private storage's UUID prefix on filesystems with byte limits.
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem or 'carta'}.pdf"


def validate_menu_pdf(data: bytes, content_type: str | None) -> None:
    if (content_type or "").split(";", 1)[0].strip().lower() != "application/pdf":
        raise HTTPException(415, "Usa un archivo PDF")
    if not data:
        raise HTTPException(422, "El PDF está vacío")
    if len(data) > MENU_PDF_MAX_BYTES:
        raise HTTPException(413, "El PDF no puede superar 8 MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(422, "El archivo no contiene un PDF válido")
    try:
        # A disposable process makes the wall-clock deadline enforceable, even for
        # parser failures that cannot be interrupted safely inside a worker thread.
        parsed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve())], input=data,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=MENU_PDF_PARSE_TIMEOUT_SECONDS, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "El PDF es demasiado complejo") from exc
    if parsed.returncode == 3:
        raise HTTPException(422, "Usa un PDF sin contraseña ni cifrado")
    if parsed.returncode == 4:
        raise HTTPException(422, "El PDF debe contener al menos una página")
    if parsed.returncode:
        raise HTTPException(422, "El PDF no es válido o es demasiado complejo")


def _parse_menu_pdf(data: bytes) -> int:
    try:
        # Scope limits to this validation; no global parser settings or content extraction.
        with apply_configuration(
            maximum_declared_stream_length=MENU_PDF_MAX_BYTES,
            zlib_maximum_output_length=16 * 1024 * 1024,
            array_based_stream_maximum_output_length=16 * 1024 * 1024,
            lzw_maximum_output_length=16 * 1024 * 1024,
            run_length_maximum_output_length=16 * 1024 * 1024,
            stream_decoding_work_maximum_length=32 * 1024 * 1024,
            array_maximum_element_count=20_000,
            dictionary_maximum_element_count=20_000,
            page_tree_maximum_entries=2_000,
            page_tree_maximum_depth=30,
            page_tree_maximum_page_entries=40_000,
        ), PdfReader(BytesIO(data), strict=True, root_object_recovery_limit=1_000,
                     xref_stream_maximum_entries=100_000) as reader:
            if reader.is_encrypted:
                return 3
            if not len(reader.pages):
                return 4
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    document = sys.stdin.buffer.read(MENU_PDF_MAX_BYTES + 1)
    sys.exit(_parse_menu_pdf(document) if 0 < len(document) <= MENU_PDF_MAX_BYTES else 2)
