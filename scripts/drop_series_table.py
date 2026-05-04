import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def _normalize_database_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    filtered_query = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "schema"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered_query), parts.fragment))


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL missing")

    engine = create_engine(_normalize_database_url(database_url))
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS "Series" CASCADE'))

    print("Dropped Series table")


if __name__ == "__main__":
    main()
