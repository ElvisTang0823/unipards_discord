import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.activity.sys_home import SysHome


class SysHomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_launches_activity_without_sending_a_button_message(self) -> None:
        response = SimpleNamespace(
            launch_activity=AsyncMock(),
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(response=response)

        await SysHome.sys_home.callback(SysHome(Mock()), interaction)

        response.launch_activity.assert_awaited_once_with()
        response.send_message.assert_not_awaited()

    async def test_reports_activity_launch_failure(self) -> None:
        response = SimpleNamespace(
            launch_activity=AsyncMock(
                side_effect=discord.HTTPException(
                    SimpleNamespace(status=500, reason="Internal Server Error"),
                    "launch failed",
                )
            ),
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(response=response)

        with self.assertLogs("cogs.activity.sys_home", level="ERROR"):
            await SysHome.sys_home.callback(SysHome(Mock()), interaction)

        response.send_message.assert_awaited_once_with(
            "目前無法啟動系統首頁 Activity，請稍後再試或聯絡管理員。",
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()
