"""Deterministic rendering only. No local OCR/model inference."""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps, ImageSequence


@dataclass(frozen=True)
class Document:
    sha256: str
    images: tuple[Path, ...]


def source_path(root: Path, name: str) -> Path:
    root = root.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"文件缺失或路径超出 files 目录: {name}")
    return path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def document_info(path: Path) -> dict:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as pdf:
            if pdf.needs_pass:
                raise ValueError("PDF 有密码")
            pages = len(pdf)
    else:
        with Image.open(path) as image:
            pages = getattr(image, "n_frames", 1)
    if pages < 1:
        raise ValueError("文件不含页面")
    return {"pages": pages, "bytes": path.stat().st_size}


def prepare_document(path: Path, cache_dir: Path, dpi: int = 180,
                     max_side: int = 2400, max_pages: int = 24) -> Document:
    info = document_info(path)
    if info["pages"] > max_pages:
        raise ValueError(f"文档有 {info['pages']} 页，超过 --max-pages={max_pages}；"
                         "请提高上限或增加页面检索，程序不会静默丢页")
    sha = file_hash(path)
    version = f"{sha}|render-v1|{dpi}|{max_side}|{fitz.VersionBind}|{Image.__version__}"
    cache = cache_dir / hashlib.sha256(version.encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    outputs = tuple(cache / f"page-{i + 1:04d}.jpg" for i in range(info["pages"]))
    if all(p.is_file() for p in outputs):
        return Document(sha, outputs)

    def save(image: Image.Image, target: Path):
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        image.save(encoded, "JPEG", quality=92, optimize=True)
        if encoded.tell() > 6 * 1024 * 1024:
            raise ValueError("单页 JPEG 超过 6MiB，请降低 --max-side")
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded.getvalue())
        temporary.replace(target)

    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as pdf:
            for page, target in zip(pdf, outputs):
                # Bound the actual raster allocation as well as the final JPEG.
                scale = min(dpi / 72, max_side / max(page.rect.width, page.rect.height))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False,
                                        colorspace=fitz.csRGB)
                save(Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples), target)
    else:
        with Image.open(path) as image:
            for frame, target in zip(ImageSequence.Iterator(image), outputs):
                save(frame.copy(), target)
    return Document(sha, outputs)


def image_content(document: Document, max_pixels: int) -> list[dict]:
    content = []
    for page_no, path in enumerate(document.images, 1):
        content.append({"type": "text", "text": f"以下是文档第 {page_no} 页："})
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"},
                        "max_pixels": max_pixels})
    return content
