from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._config import required_snowflake


BASE_DIR = Path(__file__).resolve().parents[1]
UPDATE_OWNER_ID = required_snowflake("UPDATE_OWNER_ID")
UPDATE_BRANCH = os.getenv("UPDATE_BRANCH", "master").strip()
UPDATE_TIMEOUT_SECONDS = 600
PROMPT_TIMEOUT_SECONDS = 900
logger = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    pass


class GitCheckoutError(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateStatus:
    remote_commit: str
    commits_behind: int


async def run_process(
    *arguments: str,
    timeout: int,
) -> tuple[int, str, str]:
    process_environment = os.environ.copy()
    process_environment["GIT_TERMINAL_PROMPT"] = "0"
    process_environment["GCM_INTERACTIVE"] = "Never"
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=BASE_DIR,
            env=process_environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise UpdateError(
            f"Unable to start {Path(arguments[0]).name}"
        ) from error

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise UpdateError(
            f"{Path(arguments[0]).name} timed out"
        ) from error
    if process.returncode is None:
        raise UpdateError(f"{Path(arguments[0]).name} did not exit cleanly")
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


class UpdateManager(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._prompt_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._pending_view: UpdateApprovalView | None = None
        self._dependencies_pending = False
        self._dismissed_commit: str | None = None

    async def cog_load(self) -> None:
        result, _, _ = await run_process(
            "git",
            "check-ref-format",
            "--branch",
            UPDATE_BRANCH,
            timeout=30,
        )
        if result != 0:
            raise RuntimeError("UPDATE_BRANCH is not a valid Git branch name")
        self.check_for_updates.start()

    def cog_unload(self) -> None:
        self.check_for_updates.cancel()

    @tasks.loop(hours=1)
    async def check_for_updates(self) -> None:
        try:
            status = await self.get_update_status()
            if (
                (status.commits_behind or self._dependencies_pending)
                and status.remote_commit != self._dismissed_commit
            ):
                await self.send_approval_prompt(
                    status.remote_commit,
                    retry_dependencies=self._dependencies_pending,
                )
        except GitCheckoutError as error:
            logger.error("Automatic update checks disabled: %s", error)
            self.check_for_updates.stop()
        except UpdateError:
            logger.exception("Automatic update check failed")
        except discord.HTTPException:
            logger.exception("Unable to deliver automatic update notification")

    @check_for_updates.before_loop
    async def before_check_for_updates(self) -> None:
        await self.bot.wait_until_ready()

    @commands.command(name="update")
    async def request_update(self, ctx: commands.Context) -> None:
        if (
            ctx.guild is not None
            or ctx.author.id != UPDATE_OWNER_ID
            or ctx.author.bot
        ):
            await ctx.send("此指令僅供指定管理員以私訊使用。")
            return

        async with ctx.typing():
            try:
                status = await self.get_update_status()
            except GitCheckoutError as error:
                await ctx.send(str(error))
                return
            except UpdateError:
                logger.exception("Manual update check failed")
                await ctx.send(
                    "目前無法檢查更新，請查看 Bot 主機上的日誌。"
                )
                return

        if not status.commits_behind and not self._dependencies_pending:
            await ctx.send("目前已是最新版本，沒有待安裝的更新。")
            return

        try:
            sent = await self.send_approval_prompt(
                status.remote_commit,
                retry_dependencies=self._dependencies_pending,
                channel=ctx.channel,
            )
        except discord.HTTPException:
            await ctx.send("無法傳送更新審核卡片，請稍後再試。")
            return
        if not sent:
            await ctx.send("已有待審核的更新卡片，請直接使用該卡片。")

    @app_commands.command(name="test", description="測試自動更新功能是否正常")
    async def test_update(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "自動更新程式 OK",
            ephemeral=True,
        )

    async def get_update_status(self) -> UpdateStatus:
        root_result, repository_root, git_error = await run_process(
            "git",
            "rev-parse",
            "--show-toplevel",
            timeout=30,
        )
        if root_result != 0:
            detail = f" Git 回報：{git_error}" if git_error else ""
            raise GitCheckoutError(
                "目前專案不是 Git 工作目錄，無法使用自動更新。"
                "請以 `git clone` 下載專案，不要使用 GitHub ZIP；"
                "或繼續手動啟動，但自動更新功能將無法使用。"
                + detail
            )
        if Path(repository_root).resolve() != BASE_DIR.resolve():
            raise GitCheckoutError(
                "Bot 專案資料夾不是 Git 儲存庫根目錄，無法安全自動更新。"
                "請在 Git clone 出來的專案根目錄啟動 Bot。"
            )

        branch_result, branch, _ = await run_process(
            "git",
            "branch",
            "--show-current",
            timeout=30,
        )
        if branch_result != 0:
            raise GitCheckoutError(
                "無法讀取目前 Git 分支；請確認 Bot 位於有效的 Git clone 工作目錄。"
            )
        if not branch:
            raise GitCheckoutError(
                "目前 Git 工作目錄處於 detached HEAD，無法使用自動更新。"
                "請切換到設定的本地分支後重啟 Bot。"
            )
        if branch != UPDATE_BRANCH:
            raise UpdateError(
                f"Bot is on branch {branch!r}, expected {UPDATE_BRANCH!r}"
            )

        fetch_result, _, _ = await run_process(
            "git",
            "fetch",
            "origin",
            (
                f"+refs/heads/{UPDATE_BRANCH}:"
                f"refs/remotes/origin/{UPDATE_BRANCH}"
            ),
            timeout=UPDATE_TIMEOUT_SECONDS,
        )
        if fetch_result != 0:
            raise UpdateError("Git fetch failed")

        remote_result, remote_commit, _ = await run_process(
            "git",
            "rev-parse",
            f"refs/remotes/origin/{UPDATE_BRANCH}",
            timeout=30,
        )
        if remote_result != 0:
            raise UpdateError("Unable to compare local and remote commits")

        count_result, counts, _ = await run_process(
            "git",
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...refs/remotes/origin/{UPDATE_BRANCH}",
            timeout=30,
        )
        if count_result != 0:
            raise UpdateError("Unable to compare local and remote history")
        try:
            ahead, behind = (int(value) for value in counts.split())
        except ValueError as error:
            raise UpdateError("Git returned invalid commit counts") from error
        if ahead:
            raise UpdateError(
                "Local branch has commits not present on the remote branch"
            )
        return UpdateStatus(remote_commit, behind)

    async def send_approval_prompt(
        self,
        remote_commit: str,
        *,
        retry_dependencies: bool = False,
        channel: discord.abc.Messageable | None = None,
    ) -> bool:
        async with self._prompt_lock:
            if self._pending_view is not None:
                return False
            if channel is None:
                owner = self.bot.get_user(UPDATE_OWNER_ID)
                if owner is None:
                    owner = await self.bot.fetch_user(UPDATE_OWNER_ID)
                channel = owner.dm_channel or await owner.create_dm()

            if retry_dependencies:
                description = (
                    "程式碼已更新，但相依套件尚未安裝完成。"
                    "請確認是否重試套件安裝並重新啟動。"
                )
            else:
                description = (
                    f"遠端分支 `{UPDATE_BRANCH}` 有新提交。"
                    "是否允許 Bot 暫時下線、更新程式與套件後重新啟動？"
                )
            embed = discord.Embed(
                title="Bot 更新待核准",
                description=description,
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="遠端提交",
                value=f"`{remote_commit[:12]}`",
                inline=False,
            )
            view = UpdateApprovalView(
                self,
                remote_commit,
                retry_dependencies=retry_dependencies,
            )
            try:
                await channel.send(embed=embed, view=view)
            except discord.HTTPException:
                logger.exception(
                    "Unable to send update approval prompt to configured owner"
                )
                raise
            self._pending_view = view
            return True

    def dismiss_commit(self, commit: str) -> None:
        self._dismissed_commit = commit

    async def finish_prompt(self, view: UpdateApprovalView) -> None:
        async with self._prompt_lock:
            if self._pending_view is view:
                self._pending_view = None

    async def apply_approved_update(
        self,
        expected_commit: str,
        *,
        retry_dependencies: bool,
    ) -> str:
        async with self._update_lock:
            status = await self.get_update_status()
            if status.remote_commit != expected_commit:
                return f"newer_commit:{status.remote_commit}"

            if status.commits_behind:
                result, dirty_status, _ = await run_process(
                    "git",
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                    timeout=30,
                )
                if result != 0:
                    raise UpdateError("Unable to check local working tree")
                if dirty_status:
                    raise UpdateError(
                        "Local working tree has uncommitted changes"
                    )

                merge_result, _, _ = await run_process(
                    "git",
                    "merge",
                    "--ff-only",
                    f"refs/remotes/origin/{UPDATE_BRANCH}",
                    timeout=UPDATE_TIMEOUT_SECONDS,
                )
                if merge_result != 0:
                    raise UpdateError(
                        "Fast-forward update failed; local code was not merged"
                    )
            elif not retry_dependencies and not self._dependencies_pending:
                return "already_current"

            install_result, _, _ = await run_process(
                sys.executable,
                "-m",
                "pip",
                "--no-input",
                "install",
                "-r",
                str(BASE_DIR / "requirements.txt"),
                timeout=UPDATE_TIMEOUT_SECONDS,
            )
            if install_result != 0:
                self._dependencies_pending = True
                raise UpdateError(
                    "Code was updated, but dependency installation failed"
                )

            self._dependencies_pending = False
            return "updated"


class UpdateApprovalView(discord.ui.View):
    def __init__(
        self,
        manager: UpdateManager,
        expected_commit: str,
        *,
        retry_dependencies: bool,
    ) -> None:
        super().__init__(timeout=PROMPT_TIMEOUT_SECONDS)
        self.manager = manager
        self.expected_commit = expected_commit
        self.retry_dependencies = retry_dependencies

    def disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != UPDATE_OWNER_ID:
            await interaction.response.send_message(
                "只有指定管理員可以審核此更新。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="確認更新",
        style=discord.ButtonStyle.success,
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[UpdateApprovalView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.disable_buttons()
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                logger.exception("Unable to disable update approval buttons")

        try:
            result = await self.manager.apply_approved_update(
                self.expected_commit,
                retry_dependencies=self.retry_dependencies,
            )
        except UpdateError as error:
            logger.error("Approved update failed: %s", error)
            await interaction.followup.send(
                f"更新未完成：{error}。請查看 Bot 主機日誌。"
            )
            await self.manager.finish_prompt(self)
            return

        await self.manager.finish_prompt(self)
        self.manager.dismiss_commit(self.expected_commit)
        if result.startswith("newer_commit:"):
            latest_commit = result.partition(":")[2]
            await interaction.followup.send(
                "遠端又有新提交，請重新審核最新版本。"
            )
            try:
                await self.manager.send_approval_prompt(
                    latest_commit,
                    retry_dependencies=self.manager._dependencies_pending,
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    "無法傳送新的審核卡片，請稍後私訊 Bot `!update` 重試。"
                )
            return
        if result == "already_current":
            await interaction.followup.send("目前已是最新版本，未執行更新。")
            return

        await interaction.followup.send("更新完成，Bot 即將重新啟動。")
        await self.manager.bot.close()

    @discord.ui.button(
        label="暫不更新",
        style=discord.ButtonStyle.secondary,
    )
    async def decline(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[UpdateApprovalView],
    ) -> None:
        try:
            await interaction.response.edit_message(
                content="已取消此次更新；Bot 將維持目前版本。",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            logger.exception("Unable to mark update approval as declined")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "已取消此次更新；Bot 將維持目前版本。",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "已取消此次更新；Bot 將維持目前版本。",
                    ephemeral=True,
                )
        await self.manager.finish_prompt(self)
        self.manager.dismiss_commit(self.expected_commit)

    async def on_timeout(self) -> None:
        self.disable_buttons()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="更新審核已逾時；Bot 將維持目前版本。",
                    embed=None,
                    view=self,
                )
            except discord.HTTPException:
                logger.exception("Unable to mark expired update prompt")
        await self.manager.finish_prompt(self)
        self.manager.dismiss_commit(self.expected_commit)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UpdateManager(bot))
