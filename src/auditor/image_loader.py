"""
Image Loader — загрузка изображений из CSV (URL или локальные пути).

Универсальный загрузчик: автодетект колонки с URL,
скачивание через httpx, поддержка локальных файлов.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Паттерны для автодетекта URL-колонки
_URL_COLUMN_HINTS = ["urls", "url", "image_url", "image", "link", "path", "file"]


@dataclass
class ImageRecord:
    """Одно изображение из CSV."""
    index: int
    source_url: str
    image_bytes: bytes
    metadata: dict = field(default_factory=dict)


class ImageLoader:
    """Загружает изображения из CSV (URLs или локальные пути)."""

    @staticmethod
    async def from_csv(
        csv_path: str | Path,
        *,
        url_column: str | None = None,
        limit: int | None = None,
    ) -> list[ImageRecord]:
        """
        Читает CSV, скачивает изображения.

        Args:
            csv_path: путь к CSV-файлу
            url_column: имя колонки с URL (автодетект если None)
            limit: максимум изображений для загрузки

        Returns:
            list[ImageRecord] — загруженные изображения с метаданными
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

            # Автодетект URL-колонки
            col = url_column or _detect_url_column(headers)
            if not col:
                raise ValueError(
                    f"Cannot detect URL column in CSV. Headers: {headers}. "
                    f"Use --url-column to specify manually."
                )
            logger.info(f"[loader] Using URL column: {col!r}")

            rows = list(reader)

        if limit:
            rows = rows[:limit]

        logger.info(f"[loader] Loading {len(rows)} images from {csv_path.name}")

        records: list[ImageRecord] = []
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for i, row in enumerate(rows):
                url = row.get(col, "").strip()
                if not url:
                    logger.warning(f"[loader] Row {i}: empty URL, skipping")
                    continue

                try:
                    image_bytes = await download_image(client, url)
                    metadata = {k: v for k, v in row.items() if k != col}
                    records.append(ImageRecord(
                        index=i,
                        source_url=url,
                        image_bytes=image_bytes,
                        metadata=metadata,
                    ))
                    logger.info(f"[loader] [{i+1}/{len(rows)}] Downloaded {len(image_bytes)} bytes from {url[:80]}")
                except Exception as e:
                    logger.error(f"[loader] [{i+1}/{len(rows)}] Failed to download {url[:80]}: {e}")

        logger.info(f"[loader] Loaded {len(records)}/{len(rows)} images successfully")
        return records


def _detect_url_column(headers: list[str]) -> str | None:
    """Автодетект колонки с URL по имени."""
    headers_lower = {h.lower(): h for h in headers}
    for hint in _URL_COLUMN_HINTS:
        if hint in headers_lower:
            return headers_lower[hint]
    # Fallback: ищем колонку содержащую "url" в имени
    for h in headers:
        if "url" in h.lower() or "link" in h.lower() or "path" in h.lower():
            return h
    return None


async def download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """Скачивает изображение. Поддержка http(s) и локальных путей."""
    if url.startswith(("http://", "https://")):
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
    else:
        # Локальный файл
        path = Path(url)
        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")
        return path.read_bytes()
