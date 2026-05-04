import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL missing")

    parts = urlsplit(database_url)
    filtered_query = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "schema"]
    normalized_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered_query), parts.fragment))

    engine = create_engine(normalized_url)
    tables = [
        "ImportCandidateChapter",
        "AssetNovelMapping",
        "ImportJob",
        "ImportSession",
        "SourceAsset",
    ]

    with engine.begin() as conn:
        for table in tables:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))

    print("Dropped legacy tables")


if __name__ == "__main__":
    main()
