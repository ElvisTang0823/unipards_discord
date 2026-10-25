import json
import sqlite3
from pathlib import Path


def initialize_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA foreign_keys = ON")
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vote_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL DEFAULT '',
                creator_avatar_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                options_json TEXT NOT NULL,
                max_votes INTEGER NOT NULL,
                deadline_at TEXT NOT NULL,
                webhook_id INTEGER,
                webhook_token TEXT,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closing', 'closed')),
                result_message_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        poll_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(vote_polls)")
        }
        for column_name, definition in (
            ("creator_name", "TEXT NOT NULL DEFAULT ''"),
            ("creator_avatar_url", "TEXT NOT NULL DEFAULT ''"),
            ("webhook_id", "INTEGER"),
            ("webhook_token", "TEXT"),
        ):
            if column_name not in poll_columns:
                connection.execute(
                    f"ALTER TABLE vote_polls ADD COLUMN {column_name} {definition}"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vote_ballots (
                poll_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                PRIMARY KEY (poll_id, voter_id, option_index),
                FOREIGN KEY (poll_id) REFERENCES vote_polls(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vote_polls_status_deadline
            ON vote_polls(status, deadline_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                panel_message_id INTEGER,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL,
                creator_avatar_url TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                capacity INTEGER NOT NULL DEFAULT 0,
                role_ids_json TEXT NOT NULL,
                webhook_id INTEGER,
                webhook_token TEXT,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closing', 'selecting', 'announcing', 'closed')),
                selection_deadline_at TEXT,
                selection_message_id INTEGER,
                selection_channel_id INTEGER,
                selected_user_ids_json TEXT NOT NULL DEFAULT '[]',
                announcement_message_ids_json TEXT NOT NULL DEFAULT '[]',
                organizer_notice_message_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        signup_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(signup_events)")
        }
        if "organizer_notice_message_id" not in signup_columns:
            connection.execute(
                "ALTER TABLE signup_events ADD COLUMN organizer_notice_message_id INTEGER"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_members (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                eligible_at_cutoff INTEGER,
                result TEXT CHECK (result IN ('accepted', 'waitlist', 'ineligible')),
                PRIMARY KEY (event_id, user_id),
                FOREIGN KEY (event_id) REFERENCES signup_events(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_signup_events_status_deadline
            ON signup_events(status, deadline_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                panel_message_id INTEGER,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL,
                creator_avatar_url TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                attachment_url TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                webhook_id INTEGER,
                webhook_token TEXT,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'announcing', 'closed')),
                announcement_message_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_participants (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                items TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, user_id),
                FOREIGN KEY (event_id) REFERENCES order_events(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_events_status_deadline
            ON order_events(status, deadline_at)
            """
        )
        connection.commit()
    except sqlite3.Error:
        connection.close()
        raise

    return connection


def create_vote_poll(
    connection: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    creator_name: str,
    creator_avatar_url: str,
    title: str,
    description: str,
    options_json: str,
    max_votes: int,
    deadline_at: str,
    created_at: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO vote_polls (
            guild_id, channel_id, creator_id, creator_name, creator_avatar_url,
            title, description, options_json, max_votes, deadline_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            channel_id,
            creator_id,
            creator_name,
            creator_avatar_url,
            title,
            description,
            options_json,
            max_votes,
            deadline_at,
            created_at,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def set_vote_poll_webhook(
    connection: sqlite3.Connection,
    poll_id: int,
    webhook_id: int,
    webhook_token: str,
) -> None:
    connection.execute(
        """
        UPDATE vote_polls
        SET webhook_id = ?, webhook_token = ?
        WHERE id = ?
        """,
        (webhook_id, webhook_token, poll_id),
    )
    connection.commit()


def get_open_vote_polls(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM vote_polls
            WHERE status IN ('open', 'closing')
            ORDER BY id
            """
        )
    )


def get_vote_poll(
    connection: sqlite3.Connection,
    poll_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM vote_polls WHERE id = ?",
        (poll_id,),
    ).fetchone()


def set_vote_poll_message(
    connection: sqlite3.Connection,
    poll_id: int,
    message_id: int,
) -> None:
    connection.execute(
        "UPDATE vote_polls SET message_id = ? WHERE id = ?",
        (message_id, poll_id),
    )
    connection.commit()


def delete_vote_poll(connection: sqlite3.Connection, poll_id: int) -> None:
    connection.execute("DELETE FROM vote_polls WHERE id = ?", (poll_id,))
    connection.commit()


def get_vote_selection(
    connection: sqlite3.Connection,
    poll_id: int,
    voter_id: int,
) -> list[int]:
    rows = connection.execute(
        """
        SELECT option_index FROM vote_ballots
        WHERE poll_id = ? AND voter_id = ?
        ORDER BY option_index
        """,
        (poll_id, voter_id),
    )
    return [int(row["option_index"]) for row in rows]


def replace_vote_selection(
    connection: sqlite3.Connection,
    poll_id: int,
    voter_id: int,
    option_indices: list[int],
) -> str:
    with connection:
        poll = connection.execute(
            "SELECT status, max_votes, options_json FROM vote_polls WHERE id = ?",
            (poll_id,),
        ).fetchone()
        if poll is None or poll["status"] != "open":
            return "closed"

        option_count = len(json.loads(poll["options_json"]))
        if (
            not option_indices
            or len(option_indices) > poll["max_votes"]
            or len(set(option_indices)) != len(option_indices)
            or any(index < 0 or index >= option_count for index in option_indices)
        ):
            return "invalid"

        connection.execute(
            "DELETE FROM vote_ballots WHERE poll_id = ? AND voter_id = ?",
            (poll_id, voter_id),
        )
        connection.executemany(
            """
            INSERT INTO vote_ballots (poll_id, voter_id, option_index)
            VALUES (?, ?, ?)
            """,
            (
                (poll_id, voter_id, option_index)
                for option_index in option_indices
            ),
        )
        return "saved"


def set_vote_result_message(
    connection: sqlite3.Connection,
    poll_id: int,
    result_message_id: int,
) -> None:
    connection.execute(
        "UPDATE vote_polls SET result_message_id = ? WHERE id = ?",
        (result_message_id, poll_id),
    )
    connection.commit()


def begin_vote_poll_close(
    connection: sqlite3.Connection,
    poll_id: int,
) -> sqlite3.Row | None:
    with connection:
        poll = connection.execute(
            "SELECT * FROM vote_polls WHERE id = ?",
            (poll_id,),
        ).fetchone()
        if poll is None or poll["status"] == "closed":
            return None
        connection.execute(
            "UPDATE vote_polls SET status = 'closing' WHERE id = ?",
            (poll_id,),
        )
    return poll


def get_vote_results(
    connection: sqlite3.Connection,
    poll_id: int,
) -> tuple[dict[int, int], int, int]:
    rows = connection.execute(
        """
        SELECT option_index, COUNT(*) AS vote_count
        FROM vote_ballots
        WHERE poll_id = ?
        GROUP BY option_index
        """,
        (poll_id,),
    )
    counts = {int(row["option_index"]): int(row["vote_count"]) for row in rows}
    totals = connection.execute(
        """
        SELECT COUNT(DISTINCT voter_id) AS voter_count, COUNT(*) AS ballot_count
        FROM vote_ballots WHERE poll_id = ?
        """,
        (poll_id,),
    ).fetchone()
    return counts, int(totals["voter_count"]), int(totals["ballot_count"])


def complete_vote_poll(
    connection: sqlite3.Connection,
    poll_id: int,
    result_message_id: int,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE vote_polls
            SET status = 'closed', result_message_id = ?, webhook_token = NULL
            WHERE id = ?
            """,
            (result_message_id, poll_id),
        )


def create_signup_event(
    connection: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    creator_name: str,
    creator_avatar_url: str,
    title: str,
    description: str,
    deadline_at: str,
    capacity: int,
    role_ids_json: str,
    created_at: str,
) -> int:
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO signup_events (
                guild_id, channel_id, creator_id, creator_name,
                creator_avatar_url, title, description, deadline_at,
                capacity, role_ids_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                creator_id,
                creator_name,
                creator_avatar_url,
                title,
                description,
                deadline_at,
                capacity,
                role_ids_json,
                created_at,
            ),
        )
        event_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO signup_members (
                event_id, user_id, display_name, registered_at, eligible_at_cutoff, result
            )
            VALUES (?, ?, ?, ?, 1, 'accepted')
            """,
            (event_id, creator_id, creator_name, created_at),
        )
    return event_id


def set_signup_event_webhook(
    connection: sqlite3.Connection,
    event_id: int,
    webhook_id: int,
    webhook_token: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET webhook_id = ?, webhook_token = ? WHERE id = ?",
            (webhook_id, webhook_token, event_id),
        )


def set_signup_event_message(
    connection: sqlite3.Connection,
    event_id: int,
    message_id: int,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET panel_message_id = ? WHERE id = ?",
            (message_id, event_id),
        )


def get_signup_event(
    connection: sqlite3.Connection,
    event_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM signup_events WHERE id = ?",
        (event_id,),
    ).fetchone()


def get_signup_event_by_message(
    connection: sqlite3.Connection,
    message_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM signup_events WHERE panel_message_id = ?",
        (message_id,),
    ).fetchone()


def get_open_signup_events(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM signup_events
            WHERE status IN ('open', 'closing', 'selecting', 'announcing')
            ORDER BY id
            """
        )
    )


def delete_signup_event(connection: sqlite3.Connection, event_id: int) -> None:
    with connection:
        connection.execute("DELETE FROM signup_events WHERE id = ?", (event_id,))


def add_signup_member(
    connection: sqlite3.Connection,
    event_id: int,
    user_id: int,
    display_name: str,
    registered_at: str,
) -> bool:
    with connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO signup_members (
                event_id, user_id, display_name, registered_at
            )
            SELECT ?, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM signup_events
                WHERE id = ? AND status = 'open'
            )
            """,
            (event_id, user_id, display_name, registered_at, event_id),
        )
    return cursor.rowcount > 0


def remove_signup_member(
    connection: sqlite3.Connection,
    event_id: int,
    user_id: int,
) -> bool:
    with connection:
        cursor = connection.execute(
            """
            DELETE FROM signup_members
            WHERE event_id = ? AND user_id = ?
                AND user_id != (
                    SELECT creator_id FROM signup_events WHERE id = ?
                )
                AND EXISTS (
                    SELECT 1 FROM signup_events
                    WHERE id = ? AND status = 'open'
                )
            """,
            (event_id, user_id, event_id, event_id),
        )
    return cursor.rowcount > 0


def update_signup_member_note(
    connection: sqlite3.Connection,
    event_id: int,
    user_id: int,
    note: str,
) -> bool:
    with connection:
        cursor = connection.execute(
            """
            UPDATE signup_members
            SET note = ?
            WHERE event_id = ? AND user_id = ?
                AND EXISTS (
                    SELECT 1 FROM signup_events
                    WHERE id = ? AND status = 'open'
                )
            """,
            (note, event_id, user_id, event_id),
        )
    return cursor.rowcount > 0


def get_signup_members(
    connection: sqlite3.Connection,
    event_id: int,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM signup_members
            WHERE event_id = ?
            ORDER BY registered_at, user_id
            """,
            (event_id,),
        )
    )


def reconcile_signup_members(
    connection: sqlite3.Connection,
    event_id: int,
    active_members: list[tuple[int, str, str]],
    creator_id: int,
) -> None:
    with connection:
        active_ids = {user_id for user_id, _, _ in active_members}
        active_ids.add(creator_id)
        existing_ids = {
            int(row["user_id"])
            for row in connection.execute(
                "SELECT user_id FROM signup_members WHERE event_id = ?",
                (event_id,),
            )
        }
        stale_ids = existing_ids - active_ids - {creator_id}
        connection.executemany(
            "DELETE FROM signup_members WHERE event_id = ? AND user_id = ?",
            ((event_id, user_id) for user_id in stale_ids),
        )
        for user_id, display_name, registered_at in active_members:
            connection.execute(
                """
                INSERT INTO signup_members (
                    event_id, user_id, display_name, registered_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name
                """,
                (event_id, user_id, display_name, registered_at),
            )


def set_signup_eligibility(
    connection: sqlite3.Connection,
    event_id: int,
    eligibility: dict[int, bool],
) -> None:
    with connection:
        connection.executemany(
            """
            UPDATE signup_members SET eligible_at_cutoff = ?
            WHERE event_id = ? AND user_id = ?
            """,
            (
                (int(is_eligible), event_id, user_id)
                for user_id, is_eligible in eligibility.items()
            ),
        )


def begin_signup_selection(
    connection: sqlite3.Connection,
    event_id: int,
    deadline_at: str,
    creator_id: int,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE signup_events
            SET status = 'selecting', selection_deadline_at = ?,
                selected_user_ids_json = ?
            WHERE id = ? AND status IN ('open', 'closing')
            """,
            (deadline_at, json.dumps([creator_id]), event_id),
        )
        connection.execute(
            """
            UPDATE signup_members
            SET result = CASE
                WHEN user_id = (
                    SELECT creator_id FROM signup_events WHERE id = ?
                ) THEN 'accepted'
                WHEN eligible_at_cutoff = 1 THEN 'waitlist'
                ELSE 'ineligible'
            END
            WHERE event_id = ?
            """,
            (event_id, event_id),
        )


def set_signup_selection_message(
    connection: sqlite3.Connection,
    event_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE signup_events
            SET selection_channel_id = ?, selection_message_id = ?
            WHERE id = ?
            """,
            (channel_id, message_id, event_id),
        )


def set_signup_selected_user_ids(
    connection: sqlite3.Connection,
    event_id: int,
    user_ids_json: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET selected_user_ids_json = ? WHERE id = ?",
            (user_ids_json, event_id),
        )


def finalize_signup_event(
    connection: sqlite3.Connection,
    event_id: int,
    accepted_user_ids: list[int],
    announcement_message_ids_json: str = "[]",
) -> bool:
    accepted_ids = set(accepted_user_ids)
    with connection:
        cursor = connection.execute(
            """
            UPDATE signup_events
            SET status = 'announcing',
                selected_user_ids_json = ?,
                announcement_message_ids_json = ?
            WHERE id = ? AND status IN ('closing', 'selecting')
            """,
            (
                json.dumps(sorted(accepted_ids)),
                announcement_message_ids_json,
                event_id,
            ),
        )
        if cursor.rowcount == 0:
            return False

        rows = connection.execute(
            "SELECT user_id, eligible_at_cutoff FROM signup_members WHERE event_id = ?",
            (event_id,),
        ).fetchall()
        connection.executemany(
            """
            UPDATE signup_members
            SET result = ?
            WHERE event_id = ? AND user_id = ?
            """,
            (
                (
                    "accepted"
                    if int(row["user_id"]) in accepted_ids
                    else "waitlist"
                    if row["eligible_at_cutoff"] == 1
                    else "ineligible",
                    event_id,
                    int(row["user_id"]),
                )
                for row in rows
            ),
        )
    return True


def set_signup_announcement_message_ids(
    connection: sqlite3.Connection,
    event_id: int,
    message_ids_json: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET announcement_message_ids_json = ? WHERE id = ?",
            (message_ids_json, event_id),
        )


def set_signup_organizer_notice_message_id(
    connection: sqlite3.Connection,
    event_id: int,
    message_id: int,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET organizer_notice_message_id = ? WHERE id = ?",
            (message_id, event_id),
        )


def set_signup_event_status(
    connection: sqlite3.Connection,
    event_id: int,
    status: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE signup_events SET status = ? WHERE id = ?",
            (status, event_id),
        )


def complete_signup_event(connection: sqlite3.Connection, event_id: int) -> None:
    with connection:
        connection.execute(
            """
            UPDATE signup_events
            SET status = 'closed', webhook_token = NULL
            WHERE id = ?
            """,
            (event_id,),
        )


def create_order_event(
    connection: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    creator_name: str,
    creator_avatar_url: str,
    title: str,
    description: str,
    attachment_url: str,
    deadline_at: str,
    created_at: str,
) -> int:
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO order_events (
                guild_id, channel_id, creator_id, creator_name,
                creator_avatar_url, title, description, attachment_url,
                deadline_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                creator_id,
                creator_name,
                creator_avatar_url,
                title,
                description,
                attachment_url,
                deadline_at,
                created_at,
            ),
        )
        event_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO order_participants (
                event_id, user_id, display_name, items, updated_at
            )
            VALUES (?, ?, ?, '', ?)
            """,
            (event_id, creator_id, creator_name, created_at),
        )
    return event_id


def set_order_event_webhook(
    connection: sqlite3.Connection,
    event_id: int,
    webhook_id: int,
    webhook_token: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE order_events SET webhook_id = ?, webhook_token = ? WHERE id = ?",
            (webhook_id, webhook_token, event_id),
        )


def set_order_event_message(
    connection: sqlite3.Connection,
    event_id: int,
    message_id: int,
) -> None:
    with connection:
        connection.execute(
            "UPDATE order_events SET panel_message_id = ? WHERE id = ?",
            (message_id, event_id),
        )


def get_order_event(
    connection: sqlite3.Connection,
    event_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM order_events WHERE id = ?",
        (event_id,),
    ).fetchone()


def get_open_order_events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM order_events
            WHERE status IN ('open', 'announcing')
            ORDER BY id
            """
        )
    )


def delete_order_event(connection: sqlite3.Connection, event_id: int) -> None:
    with connection:
        connection.execute("DELETE FROM order_events WHERE id = ?", (event_id,))


def save_order_items(
    connection: sqlite3.Connection,
    event_id: int,
    user_id: int,
    display_name: str,
    items: str,
    updated_at: str,
) -> bool:
    with connection:
        if not items:
            connection.execute(
                """
                DELETE FROM order_participants
                WHERE event_id = ? AND user_id = ?
                    AND EXISTS (
                        SELECT 1 FROM order_events
                        WHERE id = ? AND status = 'open'
                    )
                """,
                (event_id, user_id, event_id),
            )
            event = connection.execute(
                "SELECT status FROM order_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return event is not None and event["status"] == "open"

        cursor = connection.execute(
            """
            INSERT INTO order_participants (
                event_id, user_id, display_name, items, updated_at
            )
            SELECT ?, ?, ?, ?, ?
            WHERE EXISTS (
                SELECT 1 FROM order_events
                WHERE id = ? AND status = 'open'
            )
            ON CONFLICT(event_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                items = excluded.items,
                updated_at = excluded.updated_at
            """,
            (event_id, user_id, display_name, items, updated_at, event_id),
        )
    return cursor.rowcount > 0


def get_order_participants(
    connection: sqlite3.Connection,
    event_id: int,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM order_participants
            WHERE event_id = ?
            ORDER BY updated_at, user_id
            """,
            (event_id,),
        )
    )


def set_order_event_status(
    connection: sqlite3.Connection,
    event_id: int,
    status: str,
    expected_status: str | None = None,
) -> bool:
    with connection:
        if expected_status is None:
            cursor = connection.execute(
                "UPDATE order_events SET status = ? WHERE id = ?",
                (status, event_id),
            )
        else:
            cursor = connection.execute(
                "UPDATE order_events SET status = ? WHERE id = ? AND status = ?",
                (status, event_id, expected_status),
            )
    return cursor.rowcount > 0


def set_order_announcement_message_ids(
    connection: sqlite3.Connection,
    event_id: int,
    message_ids_json: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE order_events SET announcement_message_ids_json = ? WHERE id = ?",
            (message_ids_json, event_id),
        )


def complete_order_event(connection: sqlite3.Connection, event_id: int) -> None:
    with connection:
        connection.execute(
            """
            UPDATE order_events
            SET status = 'closed', webhook_token = NULL
            WHERE id = ?
            """,
            (event_id,),
        )


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
