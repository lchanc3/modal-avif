# 開發筆記

README 只寫怎麼用，實測數據放在 [Wiki](https://github.com/lchanc3/modal-avif/wiki/實測數據)。這裡放還沒完全驗證的功能。
設計上的取捨寫在程式碼註解裡，放在它們發生的地方：

- `convert.py`：image 為什麼用 trixie、為什麼容器內以檔案為單位平行、續跑與 `.part`、
  輸出資料夾記參數、看檔頭判斷格式、動態圖只取第一格
- `jobspec.py`：Dict 的 key 為什麼只有一個 writer、預設組合的數值怎麼挑的
- `policy.xml`：ImageMagick 白名單與資源上限

## HTTP 端點（單張，未完整驗證）

`convert.py` 的 `http`。部署後網址會印在 `modal deploy` 的輸出裡，需要 proxy auth：
在 Modal 的 workspace 設定建立 proxy auth token，請求時帶 `Modal-Key` / `Modal-Secret`。

```bash
curl -X POST --data-binary @photo.jpg -H "Modal-Key: $KEY" -H "Modal-Secret: $SECRET" "https://<workspace>--avif-convert-http.modal.run?quality=55" -o photo.avif
```

- 參數和批次模式相同（`quality`、`speed`、`depth`、`chroma`、`resize`、`strip`、`srgb`），用 query string 傳，
  沒給的用預設值。回應會帶 `X-Input-Format`、`X-Frames` 標頭。
- 程式自己設的上限是 64 MiB。**Modal 對 request body 的上限還沒實測。**
- 已確認沒帶 token 會回 401；**有帶 token 的實際轉檔還沒測。**
- `.avif` 輸入在批次模式不收（避免重複壓縮），這裡照樣收。
