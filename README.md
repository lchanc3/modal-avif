# modal-avif

用 [Modal](https://modal.com) 的 CPU 容器把圖片批次轉成 AVIF（ImageMagick 7 + libaom）。
本機只負責上傳、送件、下載；送出之後工作在雲端跑，關掉瀏覽器或關機都不影響。

實測數據與還沒完全驗證的功能見 [NOTES.md](NOTES.md)。

## 初次設定

需要 Python 3.12 與一個 Modal 帳號。

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
modal setup
modal deploy convert.py
```

改了 `convert.py` 或 `policy.xml` 之後要重新 `modal deploy convert.py`。

## 用法

### 網頁

```bat
start-web.bat
```

開 <http://127.0.0.1:8766>。上傳、送件、看進度、下載、看本月用量都在這一頁。

- 上傳可以拖資料夾進來，或填本機路徑。大量照片建議填路徑，會直接傳原檔、不先複製。
- 「送出」要按兩次才生效，因為按下去就開始花錢。
- 下載的檔案放在這個 repo 的 `out/<名稱>/`。

### CLI

一次走完（上傳 → 送件 → 等 → 下載）：

```bat
python avif.py run D:\圖片\某資料夾
```

分步做（送出後可以關機，之後再回來下載）：

```bat
python avif.py upload D:\圖片\某資料夾
python avif.py submit 某資料夾 --chroma 420
python avif.py wait <job id>
python avif.py pull <job id>
```

其他指令：`status`、`jobs`、`cancel`、`ls`、`rm`、`probe`。加 `-h` 看說明。

## 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `-q/--quality` | 90 | 1–100 |
| `--speed` | 1 | 0–9，越小越慢。speed 0 實測沒有好處，只多花 1.75 倍時間 |
| `--depth` | 8 | 8 / 10 / 12。漸層出現色帶再改 10 |
| `--chroma` | 444 | 動畫圖、插畫、截圖用 444；**照片改用 420**，檔案較小、肉眼看不出差別 |
| `--strip` | 關 | 去掉 EXIF/XMP，保留 ICC |
| `--srgb` | 關 | 依內嵌的 ICC 轉成 sRGB；不開就原樣保留 ICC |
| `--tag` | — | 輸出放到 `out/<名稱>_<tag>/`，同一批圖可以用不同參數各轉一份 |
| `--overwrite` | 關 | 刪掉整個輸出資料夾重轉 |
| `--cpu` | 4 | 每個容器的核心數，每個核心同時轉一張 |
| `--batch` | 40 | 每個容器最多轉幾張 |
| `--max-containers` | 20 | 同時最多開幾個容器 |

同一個輸出資料夾只能用一組參數。已經轉好的圖會跳過，所以取消後重送只會補剩下的；
換參數時要加 `--tag` 或 `--overwrite`。

## 成本

- 只有轉檔期間按秒計費，Volume 每月 1 TiB 以內免費。
- 參考：5 張插畫 PNG（最大 3840×2160），預設參數，一個容器 44 秒、約 $0.006。
- 工作結束時會顯示估計花費。那是估計值，實際以網頁上的「本月用量」為準。

確認沒有東西還在跑：

```bat
modal app list
modal container list
```

緊急時可以 `modal app stop avif-convert` 停掉全部，之後要重新 `modal deploy`。

## 已知限制

- 動態 GIF / WebP 只轉第一格。
- `.avif` 檔案不會重轉。
- 沒有 ICC 的 CMYK 圖用 ImageMagick 內建公式轉 RGB。印刷用的 CMYK 檔建議先在本機轉好。

## 疑難排解

| 狀況 | 原因 / 解法 |
|---|---|
| `已經用別的參數轉過` | 輸出資料夾是用另一組參數轉的。加 `--tag`，或 `--overwrite` 整個重轉 |
| 「driver 已消失」 | driver 結束了但狀態沒收尾。取消後重送，已經轉好的會沿用 |
| `Function avif-convert.run_job not found` | 還沒 `modal deploy convert.py` |
| 某些檔案失敗 | 網頁歷史紀錄或 `python avif.py status <job id>` 會列出每張的錯誤 |
| 改了 `jobspec.py` 但網頁行為沒變 | server 啟動時就載入了，要重啟 `start-web.bat` |
| Windows 主控台中文亂碼 | `chcp 65001`，或設定 `PYTHONIOENCODING=utf-8` |
