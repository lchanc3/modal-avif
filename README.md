# modal-avif

用 [Modal](https://modal.com) 的 CPU 容器把圖片批次轉成 AVIF（ImageMagick 7 + libaom）。
本機只負責上傳、送件、下載；送出之後工作在雲端跑，關掉瀏覽器或關機都不影響。

實測數據見 [Wiki](https://github.com/lchanc3/modal-avif/wiki/實測數據)，還沒完全驗證的功能見 [NOTES.md](NOTES.md)。

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

## 預設組合

網頁上的「預設組合」下拉選單，或 CLI 的 `--preset`。選了之後個別參數還是可以再改：

```bat
python avif.py run D:\圖片\某資料夾 --preset photo-2x
python avif.py submit 某資料夾 --preset photo-4x -q 70
```

| 名稱 | 用途 | quality | speed | depth | chroma | 縮放 | 每張記憶體 | 每容器張數 |
|---|---|---|---|---|---|---|---|---|
| `anime`（預設） | 插畫、動畫、截圖 | 90 | 1 | 8 | 444 | 100% | 2 GiB | 40 |
| `photo-2x` | 已經放大 4× 的真人照片，縮回 2× | 80 | 3 | 10 | 420 | 50% | 4 GiB | 40 |
| `photo-4x` | 已經放大 4× 的真人照片，保留原尺寸 | 75 | 4 | 10 | 420 | 100% | 8 GiB | 20 |

真人照片兩組實測（7 張 16384 長邊的人像，原檔 JPEG q95 共 204 MiB）：
`photo-2x` 剩 11.2 MiB（5.5%）、`photo-4x` 剩 39.4 MiB（19.3%），每張不到 $0.01。
數據與比較見 Wiki 的 [實測數據](https://github.com/lchanc3/modal-avif/wiki/實測數據)。預設組合定義在 `jobspec.py`，要加新的直接加在那裡（改完要重啟網頁）。

## 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `--preset` | anime | 見上面。下面的預設值是 `anime` 的 |
| `-q/--quality` | 90 | 1–100 |
| `--speed` | 1 | 0–9，越小越慢。speed 0 實測沒有好處，只多花 1.75 倍時間 |
| `--depth` | 8 | 8 / 10 / 12。漸層出現色帶再改 10 |
| `--chroma` | 444 | 動畫圖、插畫、截圖用 444；**照片改用 420**，檔案較小、肉眼看不出差別 |
| `--resize` | 100 | 縮放百分比，只縮不放。50 = 長寬各一半 |
| `--strip` | 關 | 去掉 EXIF/XMP，保留 ICC |
| `--srgb` | 關 | 依內嵌的 ICC 轉成 sRGB；不開就原樣保留 ICC |
| `--tag` | — | 輸出放到 `out/<名稱>_<tag>/`，同一批圖可以用不同參數各轉一份 |
| `--overwrite` | 關 | 刪掉整個輸出資料夾重轉 |
| `--cpu` | 4 | 每個容器的核心數，每個核心同時轉一張 |
| `--batch` | 40 | 每個容器最多轉幾張 |
| `--max-containers` | 20 | 同時最多開幾個容器 |
| `--mem-per-image` | 2048 | 每張分到的記憶體（MiB）。容器記憶體 = 核心數 × 這個值 |

同一個輸出資料夾只能用一組參數。已經轉好的圖會跳過，所以取消後重送只會補剩下的；
換參數時要加 `--tag` 或 `--overwrite`。

## 成本與疑難排解

只有轉檔期間按秒計費。費用參考與殘留任務檢查見 Wiki 的 [成本](https://github.com/lchanc3/modal-avif/wiki/成本)，常見狀況見 [疑難排解](https://github.com/lchanc3/modal-avif/wiki/疑難排解)。

## 已知限制

- 動態 GIF / WebP 只轉第一格。
- `.avif` 檔案不會重轉。
- 沒有 ICC 的 CMYK 圖用 ImageMagick 內建公式轉 RGB。印刷用的 CMYK 檔建議先在本機轉好。

## 授權

[GNU General Public License v3.0](LICENSE)　Copyright (C) 2026 shiho

可自由使用、修改、散布，但**散布修改後的版本時必須同樣以 GPL-3.0 公開原始碼**。

容器裡的 ImageMagick、libheif、libaom 是建置 image 時從 Debian 套件庫安裝的
（見 `convert.py` 的 `image`），本 repo 不散布它們的二進位檔。
