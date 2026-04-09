"""
DB Loader — загрузка изображений и типов объектов из MySQL.

Альтернативный источник данных (наряду с CSV).
Агент получает запрос пользователя, через IntentResolver определяет
нужный object_type, затем DBImageLoader загружает N случайных
изображений этого типа из БД.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiomysql
import httpx

from auditor import config
from auditor.image_loader import ImageRecord, download_image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ObjectType:
    """Тип объекта из таблицы object_types."""
    id: int
    object_type: str
    description: str


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

class DBPool:
    """Async MySQL connection pool. Используй как async context manager."""

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls) -> DBPool:
        pool = await aiomysql.create_pool(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            db=config.MYSQL_DATABASE,
            autocommit=True,
            minsize=1,
            maxsize=5,
        )
        logger.info(
            f"[db] Connected to MySQL: {config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DATABASE}"
        )
        return cls(pool)

    @property
    def pool(self) -> aiomysql.Pool:
        return self._pool

    async def __aenter__(self) -> DBPool:
        return self

    async def __aexit__(self, *exc) -> None:
        self._pool.close()
        await self._pool.wait_closed()
        logger.info("[db] Connection pool closed")


# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------

class ObjectTypeStore:
    """Чтение типов объектов из MySQL."""

    @staticmethod
    async def get_all(db: DBPool) -> list[ObjectType]:
        """Возвращает все типы объектов."""
        async with db.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT id, object_type, description FROM object_types ORDER BY id")
                rows = await cur.fetchall()
        return [ObjectType(id=r["id"], object_type=r["object_type"], description=r["description"] or "") for r in rows]

    @staticmethod
    async def get_by_id(db: DBPool, type_id: int) -> ObjectType | None:
        """Возвращает тип объекта по id."""
        async with db.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, object_type, description FROM object_types WHERE id = %s",
                    (type_id,),
                )
                row = await cur.fetchone()
        if not row:
            return None
        return ObjectType(id=row["id"], object_type=row["object_type"], description=row["description"] or "")

    @staticmethod
    async def find_by_name(db: DBPool, name: str) -> ObjectType | None:
        """Поиск типа объекта по имени (exact match, case-insensitive)."""
        async with db.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, object_type, description FROM object_types WHERE LOWER(object_type) = LOWER(%s)",
                    (name,),
                )
                row = await cur.fetchone()
        if not row:
            return None
        return ObjectType(id=row["id"], object_type=row["object_type"], description=row["description"] or "")


# ---------------------------------------------------------------------------
# Image loader from DB
# ---------------------------------------------------------------------------

class DBImageLoader:
    """Загрузка изображений из MySQL таблицы images."""

    @staticmethod
    async def load(
        db: DBPool,
        *,
        object_type_id: int,
        limit: int = 10,
        random_order: bool = True,
    ) -> list[ImageRecord]:
        """
        Загружает изображения определённого типа из БД.

        Args:
            db: пул соединений
            object_type_id: фильтр по типу объекта
            limit: максимум изображений
            random_order: случайный порядок (ORDER BY RAND())

        Returns:
            list[ImageRecord] — загруженные изображения
        """
        order = "ORDER BY RAND()" if random_order else "ORDER BY i.id"
        query = f"""
            SELECT i.id, i.url, ot.object_type, ot.description as type_description
            FROM images i
            JOIN object_types ot ON i.object_type_id = ot.id
            WHERE i.object_type_id = %s
            {order}
            LIMIT %s
        """

        async with db.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, (object_type_id, limit))
                rows = await cur.fetchall()

        if not rows:
            logger.warning(f"[db] No images found for object_type_id={object_type_id}")
            return []

        logger.info(f"[db] Found {len(rows)} images for object_type_id={object_type_id}, downloading...")

        records: list[ImageRecord] = []
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for i, row in enumerate(rows):
                url = row["url"]
                try:
                    image_bytes = await download_image(client, url)
                    records.append(ImageRecord(
                        index=i,
                        source_url=url,
                        image_bytes=image_bytes,
                        metadata={
                            "db_image_id": row["id"],
                            "object_type": row["object_type"],
                            "type_description": row["type_description"] or "",
                        },
                    ))
                    logger.info(f"[db] [{i + 1}/{len(rows)}] Downloaded {len(image_bytes)} bytes from {url[:80]}")
                except Exception as e:
                    logger.error(f"[db] [{i + 1}/{len(rows)}] Failed to download {url[:80]}: {e}")

        logger.info(f"[db] Loaded {len(records)}/{len(rows)} images successfully")
        return records

    @staticmethod
    async def count(db: DBPool, object_type_id: int) -> int:
        """Количество изображений данного типа в БД."""
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM images WHERE object_type_id = %s",
                    (object_type_id,),
                )
                row = await cur.fetchone()
        return row[0] if row else 0
