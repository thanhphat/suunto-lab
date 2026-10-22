# Suunto Lab

Hệ thống tự host để lưu và phân tích dữ liệu đồng hồ Suunto (về sau thêm Garmin, Coros), gồm ba phần:

- **Backend xử lý file FIT**: nhận file, lưu bản gốc, parse và tính chỉ số, ghi vào Postgres/TimescaleDB.
- **Web dashboard**: xem dữ liệu, thay cho việc Suunto không có bản web.
- **MCP server**: cho Claude hoặc AI agent khác đọc và phân tích dữ liệu, chỉ có quyền đọc.

```
iPhone: Suunto app → Export FIT → Share → Shortcut ─┐
Web: kéo thả file FIT ──────────────────────────────┼─▶ api  POST /api/upload
(sau này) Suunto/Garmin webhook ────────────────────┘        │  lưu file gốc (SHA-256), NOTIFY
                                                             ▼
                                worker: parse FIT → làm sạch → chỉ số → DB
                                                             │
                     Postgres + TimescaleDB ◀────────────────┘
                        ▲            ▲
                api + web (8000)    mcp (8001, read-only) ◀── Claude Desktop / claude.ai / Claude Code
```

## Chạy

```bash
cp .env.example .env        # điền mật khẩu và token (openssl rand -hex 24)
docker compose up -d --build
open http://localhost:8000  # nhập API_TOKEN một lần
```

Muốn truy cập từ điện thoại và claude.ai, cần public ra ngoài qua Cloudflare Tunnel:

1. Trong Cloudflare Zero Trust, tạo một tunnel và lấy token, điền vào `CLOUDFLARE_TUNNEL_TOKEN`.
2. Thêm hai public hostname:
   - `run.<domain>` trỏ tới `http://api:8000`
   - `mcp.<domain>` trỏ tới `http://mcp:8001`
3. Đặt `MCP_ALLOWED_HOSTS=mcp.<domain>`.
4. Chạy `docker compose --profile tunnel up -d`.

## Đưa dữ liệu vào

**Từ web:** bấm *Nhập file FIT* hoặc kéo thả nhiều file cùng lúc. File trùng sẽ tự bị bỏ qua.

**Từ iPhone (iOS Shortcut):** cách nhanh nhất là gửi thẳng từ Suunto app.

1. Mở app Shortcuts, tạo shortcut mới tên "Gửi Suunto Lab".
2. Vào phần cài đặt của shortcut, bật *Show in Share Sheet* và chọn nhận *Files*.
3. Thêm action **Get Contents of URL**:
   - URL: `https://run.<domain>/api/upload/raw?name=suunto.fit`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <API_TOKEN>`, `Content-Type` = `application/octet-stream`
   - Request Body: **File**, chọn *Shortcut Input*
4. Thêm action **Show Notification** để hiện kết quả.

Từ đó, trong Suunto app chỉ cần mở bài tập, bấm ⋯, chọn Export, chọn FIT, rồi Share và chọn *Gửi Suunto Lab*.

**Bằng curl:**

```bash
curl -H "Authorization: Bearer $API_TOKEN" -F "files=@run.fit" https://run.<domain>/api/upload
```

API docs: `/api/docs`.

## Kết nối AI agent (MCP)

Các tool có sẵn:

| Tool | Chức năng |
|---|---|
| `get_overview` | Tổng quan hiện tại |
| `list_activities` | Danh sách hoạt động |
| `get_activity` | Chi tiết một hoạt động |
| `get_activity_streams` | Chuỗi dữ liệu theo thời gian của một hoạt động |
| `get_training_load` | Tải tập luyện theo ngày |
| `get_weekly_summary` | Tổng hợp theo tuần |
| `compare_periods` | So sánh hai giai đoạn |
| `describe_schema` | Mô tả các bảng trong DB |
| `query_sql` | Chạy SQL tuỳ ý |

`query_sql` chạy bằng role `agent_ro`: chỉ đọc, timeout 8 giây, tối đa 1000 dòng, không thấy bảng `fit_files`.

**claude.ai (web, điện thoại):** vào Settings → Connectors → Add custom connector, nhập URL
`https://mcp.<domain>/mcp/<MCP_TOKEN>`. Token nằm ngay trong URL, nên giữ URL này như mật khẩu và đổi token nếu lộ.

**Claude Code:**

```bash
claude mcp add --transport http suunto-lab https://mcp.<domain>/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

**Claude Desktop (local, stdio):** thêm vào `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "suunto-lab": {
      "command": "docker",
      "args": ["compose", "-f", "/đường/dẫn/suunto-lab/docker-compose.yml", "exec", "-T", "mcp",
               "python", "-m", "app.mcp_server", "--stdio"]
    }
  }
}
```

Câu hỏi mẫu cho agent:
- "So sánh 4 tuần gần nhất với 4 tuần trước đó: volume, D+, VAM, decoupling."
- "Buổi dài nhất tháng này, nhịp tim trôi bao nhiêu ở nửa sau? Đoạn nào pace GAP tụt mạnh nhất?"
- "Với CTL/ATL hiện tại, tuần tới nên tăng hay giảm tải?"

Kiểm tra nhanh từ terminal: `MCP_TOKEN=... scripts/mcp_call.sh get_overview`.

## Thiết kế dữ liệu

Dữ liệu chia ba tầng. Tầng dưới là nguồn, tầng trên luôn tính lại được từ tầng dưới.

| Tầng | Bảng | Ghi chú |
|---|---|---|
| Thô | `fit_files` | File gốc lưu trên đĩa theo SHA-256. Có trạng thái `pending`/`processing`/`done`/`failed`/`duplicate`. |
| Chuẩn hoá | `activities`, `laps`, `samples`, `rr_intervals` | Không phụ thuộc hãng. `samples` là hypertable, khoảng 1 dòng mỗi giây. |
| Dẫn xuất | `activity_metrics`, `splits`, `daily_load` | GAP, VAM, vùng HR, TRIMP, decoupling, CTL/ATL/TSB. |
| View | `v_activities`, `v_weekly` | Điểm bắt đầu cho người và AI. |

- **Sửa thuật toán:** gọi `POST /api/reprocess-all` để parse lại toàn bộ từ file gốc. Activity id giữ nguyên.
- **Đổi HRmax hoặc vùng:** `PUT /api/settings` sẽ tính lại vùng, TRIMP và CTL/ATL cho toàn bộ lịch sử. Lưu được nhờ `hr_hist` giữ số giây ở từng nhịp tim.
- **Định nghĩa chỉ số:** xem docstring đầu file `app/metrics.py`. AI agent cũng nhận được phần này trong instructions của MCP.

## Phát triển

```bash
uv sync                                            # cài Python 3.15 và thư viện vào .venv
export DATABASE_URL=postgresql://suunto:suunto@localhost:5432/suunto API_TOKEN=dev MCP_TOKEN=dev
uv run uvicorn app.api:app --reload --port 8000     # web + API
uv run python -m app.worker                         # hàng đợi
uv run python -m app.mcp_server --http 8001         # MCP
```

Thêm hoặc nâng thư viện bằng `uv add <gói>` / `uv lock --upgrade`, rồi commit cả `pyproject.toml` và `uv.lock`.

`samples/` chứa vài file FIT **tổng hợp** (do script tạo ra) để chạy thử. Đây không phải dữ liệu thật.

## Hướng phát triển tiếp

- **Adapter Suunto Cloud API:** OAuth và webhook. Chỉ cần thêm `source='suunto_api'`, pipeline phía sau không đổi. Làm tương tự cho Garmin và Coros.
- **Nhiều người dùng:** schema đã có `athlete_id`. Cần thêm đăng nhập và OAuth cho MCP thay vì dùng token chung.
- **Chỉ số riêng cho trail:** dự đoán thời gian về đích theo profile độ cao của giải, và phân tích HRV từ `rr_intervals`.
