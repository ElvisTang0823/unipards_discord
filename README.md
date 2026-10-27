# Unipards Discord Bot

以 Python 建立的 Discord Bot 起始專案。指令功能放在 `cogs/`，預設使用 `pgembed` 在專案內啟動 PostgreSQL 17；也可透過 `DATABASE_URL` 連線到託管 PostgreSQL。

## 環境需求

- Python 3.14.8（需有 `pgembed` 對應作業系統／架構的套件 wheel；已驗證 Windows x64 的相依安裝及資料庫 smoke test）
- Discord Bot Token
- 不需預先安裝 PostgreSQL Server 或建立資料庫

## 安裝與啟動

1. 建立並啟用虛擬環境：

   ```powershell
   py -3.14 --version
   py -3.14 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   建立前請確認版本輸出為 `Python 3.14.8`。

2. 安裝套件：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 複製 `.env.example` 為 `.env`，並填入 Discord Bot Token、各項頻道／身分組 ID，以及更新管理員 ID：

   ```env
   DISCORD_TOKEN=你的BotToken
   ARCHIVE_CATEGORY_ID=封存分類ID
   PRIVATE_GROUP_CATEGORY_ID=私聊群分類ID
   ACTIVITY_PROJECT_CATEGORY_ID=活動討論群分類ID
   PROTECTED_ROLE_IDS=受保護身分組ID
   RSS_CHANNEL_ID=RSS通知頻道ID
   RSS_ROLE_ID=RSS通知身分組ID
   ROLE_REQUEST_CHANNEL_ID=入群資料頻道ID
   ROLE_REQUEST_NOTIFICATION_CHANNEL_ID=管理員通知頻道ID
   ROLE_REQUEST_ADMIN_ROLE_IDS=管理員身分組ID,另一個管理員身分組ID
   TEMP_CHANNEL_ENTRY_ID=臨時群組入口頻道ID
   TEMP_VOICE_TRIGGER_CHANNEL_ID=臨時語音入口頻道ID
   UPDATE_OWNER_ID=你的Discord使用者ID
   UPDATE_BRANCH=master
   # Optional: use the hosting provider's PostgreSQL instead of embedded PostgreSQL.
   # DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE
   ```

   `pip install -r requirements.txt` 會安裝 Psycopg 用戶端與 `pgembed` PostgreSQL Server。未設定 `DATABASE_URL` 時，Bot 會在專案內的 `data/postgres/` 啟動 PostgreSQL 並初始化 schema，不需要預先安裝資料庫或設定連接埠。若託管平台提供 PostgreSQL，請在 `.env` 或平台環境變數設定 `DATABASE_URL`，Bot 會改連該資料庫並初始化所需 schema，不啟動內嵌 PostgreSQL。請勿將含密碼的連線字串提交到版本控制。原 SQLite 資料庫已備份到 `data/backups/bot.sqlite3`，不會匯入新資料庫。

   `.env` 已加入 Git 忽略清單；請只在本機填入實際 ID 與憑證。Bot 僅支援 Python 3.14.8，啟動時會檢查版本。

   `data/` 已加入 `.gitignore`，包含 PostgreSQL 資料與舊資料備份，不會隨原始碼提交。打包或複製資料前請先完整停止 Bot；PostgreSQL 原生資料目錄只能在相容作業系統／架構及相同 PostgreSQL 主要版本間直接搬移，跨平台需用 `pg_dump`／還原。線上託管只有在主機提供持久化磁碟，且支援該平台的 `pgembed` wheel 時才適合此模式；暫存磁碟可能在重新部署時清空資料。

4. 在 Discord Developer Portal 的 Bot 設定中啟用 **Message Content Intent**，並邀請 Bot 加入伺服器。

5. 啟動 Bot：

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

### 本機核准更新與自動重啟

Bot 每小時會從 `origin` 抓取 `.env` 中 `UPDATE_BRANCH` 指定的分支。發現新提交時，會私訊 `UPDATE_OWNER_ID` 顯示審核卡片；只有按下「確認更新」才會檢查工作目錄、以 fast-forward 方式更新程式、安裝 `requirements.txt` 並關閉 Bot。管理員也可私訊 Bot `!update` 立即檢查；這不會略過按鈕核准。拒絕或 15 分鐘內未審核都不會更新。有未提交的本機變更時會中止，不會 stash 或覆寫。

更新主機須已設定可讓 Git 非互動式存取 `origin` 的憑證。專案根目錄的 `run_bot.bat` 使用 `.venv` 中的 Python 啟動 Bot，程序結束後等待 3 秒再啟動；更新完成時由此腳本載入新程式。

### 僅啟動投票功能進行測試

在 PowerShell 執行以下指令，只載入 `vote` Cog：

```powershell
$env:BOT_COGS = ""
.\.venv\Scripts\python.exe main.py
```

可用空格或逗號指定多個 Cog，例如 `$env:BOT_COGS = "vote info"`。隔離模式只載入並同步所選 Cog 的全域斜線指令，並移除伺服器中與這些指令同名的伺服器專屬版本。請使用獨立的測試 Bot Token；此模式會更新該 Bot Application 的全域斜線指令，Discord 顯示新指令可能需要一些時間。
若要單獨測試表符報名，請改用 `$env:BOT_COGS = "signup"`。
若要單獨測試揪團，請使用 `$env:BOT_COGS = "order"`。

清除 `BOT_COGS` 設定後再執行 Bot，則會載入所有功能：

```powershell
Remove-Item Env:BOT_COGS
.\.venv\Scripts\python.exe main.py
```

## 目前指令

- `!ping`：回覆 Bot 的延遲。
- `!update`：指定管理員以私訊檢查更新，並透過確認按鈕核准更新與重啟。
- `/info`：在伺服器任何頻道讀取 `commands.json` 並以私人回覆分頁顯示可用指令，可透過上一頁／下一頁按鈕翻頁。可提供指令名稱（例如 `/info command:/create-vote`）查詢單一指令的用法與說明。
- `/create-channel <頻道類型> [頻道名稱]`：在指定建頻入口建立「私聊群」或「活動討論群」。
- `/create-signup`：建立表符報名活動；建立者自動報名，成員以 ✅ 加入、移除 ✅ 取消，可填寫僅建立者可見的備註，修改時會帶入上次內容。可設定名額上限；超額時由建立者在私訊中挑選錄取者，未於 30 分鐘內完成或無法私訊時依報名順序遞補。截止後直接將原報名卡更新為結果，卡片外提及發起者並顯示「報名「標題」已截止，結果如下：」，並私訊發起者結果公告連結；錄取與候補名單在卡片內提及成員（無候補時不顯示候補卡）；名單每 15 人分頁，卡片按鈕會為每位使用者開啟獨立私人分頁。不限制身分組。活動說明同樣支援實際換行或輸入 `\n`。卡片標題格式為「報名｜{標題}」／「報名結果｜{標題}」，固定使用 `#5765F2`，頁尾統一顯示截止時間與狀態。
- `/create-vote`：建立投票；成員透過私人下拉選單選擇並可覆蓋原選擇，截止後直接編輯原投票卡公布票數與結果，移除投票按鈕，並在同一則訊息提及發起者及私訊發起者結果公告連結。投票卡片會以發起者的伺服器暱稱與 Discord 頭貼發送；投票說明支援多行，實際換行或輸入 `\n` 都會顯示為換行。卡片標題格式為「投票｜{標題}」／「投票結果｜{標題}」，固定使用 `#FAC84B`，頁尾統一顯示截止時間與狀態。
- `/create-order`：建立揪團並設定截止時間；菜單圖片可不填，但必須有圖片或非空白說明文字，否則不會建立。說明可直接輸入文字、多行內容或 `\n` 分行，不強制必須是連結。建立者預設自動加入，也可透過按鈕登記品項。成員按鈕開啟表單，以一行一個品項與價格登記多項；再次提交會覆蓋，空白提交會清除登記。截止後直接編輯原揪團卡，卡片 Embed 說明會接續列出 `<@成員>` 與 `└ 品項` 明細，卡片外提及發起者並顯示「揪團「標題」已截止，結果如下：」，並私訊發起者結果公告連結；每頁最多 15 行（包含成員提及與品項），每位使用者每段最多列 14 個品項，超過時會在下一頁重複提及該使用者，避免將每頁撐超過 15 行。品項文字中的 Markdown 符號會跳脫，避免被當成格式解析。多頁明細可由卡片按鈕各自開啟私人分頁。卡片標題格式為「揪團｜{標題}」／「揪團結果｜{標題}」，固定使用 `#E86060`，頁尾統一顯示截止時間與狀態。
- `!allow <成員|身分組|everyone>`：在自動建立的頻道允許對象存取。
- `!deny <成員|身分組|everyone>`：在自動建立的頻道拒絕對象存取。
- `!reset <成員|身分組|everyone>`：重設對象的頻道權限覆寫。
- `!list`：列出目前自動建立頻道明確允許與拒絕（包括使用 `!deny` 拒絕）的成員、身分組，以及發言／語音連線權限。
- `!close`：由建立者將活動討論群移至封存分類；封存後只有原本明確獲准檢視的對象可讀取歷史，且不可發言，Bot 會解除追蹤。臨時群組／個人語音頻道則會刪除。

Bot 會每分鐘檢查 FIRST FRC Community RSS 的最新三則文章，並在頻道 `1555919971002089595` 發送尚未發布的卡片；訊息會顯示「FRC部落格更新囉！來看看最新的文章~」並提及身分組 `1555933526665334977`。卡片顯示文章標題、RSS 圖片與原文連結，footer 使用 RSS GUID 去重，顏色為 `#ED1C24`。此功能不使用資料庫；每次啟動會讀取該頻道訊息中的 RSS footer 建立記憶體索引，並依最新三則文章補發缺少的項目。

加入指定語音入口頻道（`1556272392458735668`）會自動建立個人語音頻道並將成員移入；頻道空置後會自動刪除。自動建頻的建立者資料存放於 `channel_owners` 資料表。

啟動時會自動載入 `cogs/` 內的 Cog，並在專案內的 PostgreSQL 資料庫建立 `bot_settings`、`channel_owners`、`vote_polls`、`vote_ballots`、`signup_events`、`signup_members`、`order_events`、`order_participants` 與 `command_rate_limits` 資料表。
每位使用者（跨伺服器）的非建立指令共用「任一滾動 1 秒最多 2 次」限流；`/create-channel`、`/create-vote`、`/create-signup`、`/create-order` 共用每小時一次限流，且不另設伺服器活動上限。限制在 PostgreSQL 中原子記錄，可跨 Bot 重啟及多個 Bot 程序生效。資料庫操作使用最多 4 個背景工作者及有界佇列；佇列滿載時會拒絕新資料庫工作，不會無限制累積。此限制只約束指令執行，不限制按鈕、選單、表單、反應事件或到達 Discord／主機的網路流量，也不能取代 Discord 或主機商的 DDoS 防護。
Slash Command 會在啟動時同步至 Discord；新增或修改指令後，請同步更新 `commands.json`。
啟動時會重新同步目前載入的全域斜線指令定義，令 Discord 端與程式保持一致；並移除每個已加入伺服器內與目前全域指令同名的伺服器專屬指令，避免舊版本遮蔽新版。未載入的全域舊指令會由全域同步移除。
Bot 上線後會顯示「正在玩 FRC6998團隊的24小時助理機器人」活動狀態。

建頻功能依賴設定好的 Discord 分類與入口頻道 ID。Bot 需在相關分類／頻道擁有管理頻道權限；自動語音頻道另需移動成員權限。`!allow`、`!deny`、`!reset` 會設定頻道權限覆寫，Bot 需有管理身分組權限。前綴指令依賴 Message Content Intent。

`/create-vote` 需要 Bot 在投票頻道具有「管理 Webhook」權限，以投票發起者的伺服器暱稱與 Discord 頭貼建立投票卡，並在截止時編輯原卡公布結果。每個投票會建立專用 Webhook，投票結束後會刪除；Webhook 憑證僅在投票開放期間保存在 PostgreSQL，並於完成後清除。

`/create-signup` 需要 Bot 在活動頻道具有「管理 Webhook」、「檢視頻道」、「傳送訊息」、「嵌入連結」、「新增反應」及「讀取訊息歷史」權限；若在討論串建立活動，也需能在討論串傳送訊息。報名卡片及公告會以建立者的伺服器暱稱與 Discord 頭貼發送。截止時會重新讀取 ✅ 反應以確認最終名單，並在活動結束後清除 Webhook 憑證。報名資料保存在 PostgreSQL 的 `signup_events` 與 `signup_members` 資料表。

`/create-order` 需要 Bot 在揪團頻道具有「管理 Webhook」、「檢視頻道」及「傳送訊息」權限；若在討論串建立，也需能在討論串傳送訊息。菜單圖片可選填，但必須有圖片或非空白說明文字（可多行），未提供兩者時不會建立。截止時會直接編輯原揪團卡：Embed 說明文字先列揪團說明，再接續以 `<@成員>` 和 `└ 品項` 列出明細；一般訊息內容只提及發起者。名單多頁時可從卡片按鈕各自開啟私人分頁。揪團資料保存在 `order_events` 與 `order_participants` 資料表，Webhook 憑證於完成後清除。

`/create-signup` 會直接編輯原報名卡公布結果；卡片內提及錄取與候補成員，卡片外提及發起者。`/create-vote` 會直接編輯原投票卡公布結果 Embed，並在同一則訊息提及發起者。

Bot 啟動時會檢查卡片頻道是否已有帶有專用 footer 的身分組設定卡片；只有找不到時才會發佈。Bot 需要能讀取卡片頻道歷史並發送訊息，也需要能在管理員通知頻道查看及發送訊息。持久化按鈕會在 Bot 重啟後繼續有效。已設定伺服器暱稱的成員無法開啟姓名表單；未設定暱稱的成員提交姓名後，Bot 會嘗試以該姓名設定伺服器暱稱，並通知管理員協助設定身分組。此功能需要 Bot 有「管理暱稱」權限，且 Bot 的角色順位高於該成員。管理員設定身分組後，指定管理員之一對通知按下 ✅，Bot 便會將通知更新為已設定完成。Bot 需具備新增反應與編輯自己訊息的權限。

執行紀錄會同時輸出在終端與 `logs/` 資料夾。每次啟動都會建立新的 `bot_時間戳.log` 檔案；日誌檔不會提交到 Git。
