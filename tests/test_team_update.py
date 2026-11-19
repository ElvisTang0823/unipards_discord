import unittest

from cogs._gas_config import CONFIG_VALUES

CONFIG_VALUES.update(
    {
        "NOTIFY_CHANNEL_ID": "1",
        "NOTIFY_ROLE_ID": ["2"],
    }
)

from cogs.notifies.team_update import (
    TeamUpdate,
    parse_team_updates,
    select_pending_team_updates,
)


class TeamUpdateParsingTests(unittest.TestCase):
    def test_extracts_team_update_number_and_deduplicates_links(self) -> None:
        page = """
        <a href="https://firstfrc.blob.core.windows.net/frc2026/Manual/TeamUpdates/REBUILT_TeamUpdate22.pdf">
          <span>Team Update 22</span>
        </a>
        <a href="https://firstfrc.blob.core.windows.net/frc2026/Manual/TeamUpdates/REBUILT_TeamUpdate22.pdf">
          Team Update 22
        </a>
        <a href="https://firstfrc.blob.core.windows.net/manual/combined.pdf">
          Team Updates - Combined
        </a>
        <a href="http://example.test/TeamUpdate23.pdf">Team Update 23</a>
        """

        self.assertEqual(
            parse_team_updates(page),
            [
                TeamUpdate(
                    number=22,
                    link=(
                        "https://firstfrc.blob.core.windows.net/frc2026/"
                        "Manual/TeamUpdates/REBUILT_TeamUpdate22.pdf"
                    ),
                )
            ],
        )

    def test_first_run_selects_only_latest_update(self) -> None:
        updates = [
            TeamUpdate(21, "https://example.test/21.pdf"),
            TeamUpdate(22, "https://example.test/22.pdf"),
        ]

        self.assertEqual(
            select_pending_team_updates(updates, set()),
            [updates[1]],
        )

    def test_later_runs_select_every_unseen_update_in_order(self) -> None:
        updates = [
            TeamUpdate(21, "https://example.test/21.pdf"),
            TeamUpdate(22, "https://example.test/22.pdf"),
            TeamUpdate(23, "https://example.test/23.pdf"),
        ]

        self.assertEqual(
            select_pending_team_updates(
                updates,
                {"https://example.test/21.pdf"},
            ),
            updates[1:],
        )


if __name__ == "__main__":
    unittest.main()
