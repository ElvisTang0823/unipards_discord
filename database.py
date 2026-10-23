import sqlite3
from pathlib import Path


def initialize_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_owners (
                feature TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                PRIMARY KEY (feature, channel_id)
            )
            """
        )
        connection.commit()
    except sqlite3.Error:
        connection.close()
        raise

    return connection


def load_channel_owners(
    connection: sqlite3.Connection,
    feature: str,
) -> dict[int, int]:
    rows = connection.execute(
        "SELECT channel_id, owner_id FROM channel_owners WHERE feature = ?",
        (feature,),
    )
    return {int(row["channel_id"]): int(row["owner_id"]) for row in rows}


def load_all_channel_owners(connection: sqlite3.Connection) -> dict[int, int]:
    rows = connection.execute(
        "SELECT channel_id, owner_id FROM channel_owners"
    )
    return {int(row["channel_id"]): int(row["owner_id"]) for row in rows}


def save_channel_owners(
    connection: sqlite3.Connection,
    feature: str,
    owners: dict[int, int],
) -> None:
    with connection:
        connection.execute(
            "DELETE FROM channel_owners WHERE feature = ?",
            (feature,),
        )
        connection.executemany(
            """
            INSERT INTO channel_owners (feature, channel_id, owner_id)
            VALUES (?, ?, ?)
            """,
            (
                (feature, channel_id, owner_id)
                for channel_id, owner_id in owners.items()
            ),
        )
