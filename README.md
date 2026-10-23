# Unipards Discord Bot

以 Python 建立的 Discord Bot 起始專案。指令功能放在 `cogs/`，資料儲存使用 Python 內建的 SQLite。

## 環境需求

- Python 3.10 或更新版本
- Discord Bot Token

## 安裝與啟動

1. 建立並啟用虛擬環境：

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. 安裝套件：

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. 複製 `.env.example` 為 `.env`，並填入 Discord Bot Token：

   ```env
   DISCORD_TOKEN=你的BotToken
   ```

4. 在 Discord Developer Portal 的 Bot 設定中啟用 **Message Content Intent**，並邀請 Bot 加入伺服器。

5. 啟動 Bot：

   ```powershell
   py bot.py
   ```

## 目前指令

- `!ping`：回覆 Bot 的延遲。
- `/info`：僅限在頻道 `1556259986278195280` 讀取 `commands.json` 並顯示可用指令。
- `/create-channel <頻道類型> [頻道名稱]`：在指定建頻入口建立「私聊群」或「活動討論群」。
- `!allow <成員|身分組|everyone>`：在自動建立的頻道允許對象存取。
- `!deny <成員|身分組|everyone>`：在自動建立的頻道拒絕對象存取。
- `!reset <成員|身分組|everyone>`：重設對象的頻道權限覆寫。
- `!list`：列出目前自動建立頻道明確允許與拒絕（包括使用 `!deny` 拒絕）的成員、身分組，以及發言／語音連線權限。
- `!close`：由建立者將活動討論群移至封存分類；封存後只有原本明確獲准檢視的對象可讀取歷史，且不可發言，Bot 會解除追蹤。臨時群組／個人語音頻道則會刪除。

加入指定語音入口頻道（`1556272392458735668`）會自動建立個人語音頻道並將成員移入；頻道空置後會自動刪除。自動建頻的建立者資料存放於 `channel_owners` 資料表。

啟動時會自動載入 `cogs/` 內的 Cog，並在 `database/bot.sqlite3` 建立資料庫、`bot_settings` 與 `channel_owners` 資料表。
Slash Command 會在啟動時同步至 Discord；新增或修改指令後，請同步更新 `commands.json`。
`/info` 僅能在頻道 `1556259986278195280` 使用。啟動時會移除同名的舊伺服器範圍 `/info` 指令，避免它和全域指令同時出現。

建頻功能依賴設定好的 Discord 分類與入口頻道 ID。Bot 需在相關分類／頻道擁有管理頻道權限；自動語音頻道另需移動成員權限。`!allow`、`!deny`、`!reset` 會設定頻道權限覆寫，Bot 需有管理身分組權限。前綴指令依賴 Message Content Intent。

Bot 啟動時會檢查卡片頻道是否已有帶有專用 footer 的身分組設定卡片；只有找不到時才會發佈。Bot 需要能讀取卡片頻道歷史並發送訊息，也需要能在管理員通知頻道查看及發送訊息。持久化按鈕會在 Bot 重啟後繼續有效。已設定伺服器暱稱的成員無法開啟姓名表單；未設定暱稱的成員提交姓名後，Bot 會嘗試以該姓名設定伺服器暱稱，並通知管理員協助設定身分組。此功能需要 Bot 有「管理暱稱」權限，且 Bot 的角色順位高於該成員。管理員設定身分組後，指定管理員之一對通知按下 ✅，Bot 便會將通知更新為已設定完成。Bot 需具備新增反應與編輯自己訊息的權限。

執行紀錄會同時輸出在終端與 `logs/` 資料夾。每次啟動都會建立新的 `bot_時間戳.log` 檔案；日誌檔不會提交到 Git。
