from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


def _normalize_database_url(url: str) -> str:
    # Strip Prisma-only query params (e.g. ?schema=public) that asyncpg doesn't accept
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

    _PRISMA_ONLY_PARAMS = {"schema", "connection_limit", "pool_timeout", "connect_timeout", "sslmode"}

    if url.startswith("postgresql+asyncpg://"):
        scheme = "postgresql+asyncpg"
    elif url.startswith("postgresql://") or url.startswith("postgres://"):
        scheme = "postgresql+asyncpg"
    else:
        return url

    parsed = urlparse(url)
    clean_params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _PRISMA_ONLY_PARAMS]
    clean_url = urlunparse((
        scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        urlencode(clean_params),
        parsed.fragment,
    ))
    return clean_url


engine = create_async_engine(_normalize_database_url(settings.database_url), pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db_session() -> AsyncSession:
    session = SessionLocal()
    try:
        yield session
    finally:
        await session.close()
