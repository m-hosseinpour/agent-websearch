# websearch — جست‌وجوی وب برای عامل، روی HTTP و MCP

یک سرویس محلی که جای رلهٔ DDG و SearXNG را می‌گیرد. یک نقطهٔ اتصال، دو بک‌اند بدون کلید
(DDG و Mwmbl)، و خروجی‌ای که همیشه می‌گوید هر بک‌اند چه شد.

- **MCP:** `POST /mcp` (streamable HTTP، stateless) با یک ابزار: `search`
- **REST:** `GET|POST /search` برای curl و اسکریپت
- **سلامت:** `GET /healthz` (بدون توکن)
- هر دو مسیر یک مدل پاسخ می‌دهند؛ چیزی که curl می‌بیند همان چیزی است که عامل می‌گیرد.

طراحی و دلیل تصمیم‌ها در پوشهٔ پژوهشی `harness-comparision` است:
`research/G-mcp-search-tool-design.md`. محل محلی این ریپو: `~/workspace/websearch-mcp`.

## نیازها

- Python 3.11 یا بالاتر
- `uv` (یا pip)
- xray — اگر نباشد، اولین درخواست پروکسی‌شده یک نسخهٔ رسمی را دانلود و با SHA-256
  بررسی می‌کند. اگر از قبل دارید، با `WEBSEARCH_XRAY_BIN` یا `XRAY_BIN` معرفی کنید.

## نصب

```bash
cd websearch-mcp
uv tool install .          # مسیر اصلی: فرمان websearch در ~/.local/bin
```

برای توسعه (و مسیری که باید در unit بگذارید):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
# ExecStart=%h/path/to/websearch-mcp/.venv/bin/websearch serve
```

برای تست: `uv pip install --python .venv/bin/python pytest pytest-asyncio`

> این پوشه ریپوی مستقل خودش را دارد (نه زیرمجموعهٔ `harness-comparision`). اگر پوشه
> جابه‌جا شد، نصب editable را دوباره اجرا کنید
> (`uv pip install --python .venv/bin/python -e .`)؛ وگرنه venv هنوز به مسیر قدیمی
> اشاره می‌کند و تست‌ها با `ModuleNotFoundError` می‌افتند.

## راه‌اندازی اولیه

```bash
websearch setup
# اگر از قبل فهرست لینک دارید:
websearch setup --seed-from ~/.config/proxy-tunnel/pool.txt
```

این فرمان در `~/.config/websearch/` می‌سازد:

| فایل | کار |
|---|---|
| `config.toml` | تنظیمات (نمونه: `config.example.toml`) |
| `token` | توکن Bearer، حالت ۰۶۰۰ |
| `pool.txt` | لینک‌های `vless://` یا `trojan://`، خط اول اول امتحان می‌شود |

بقیهٔ فایل‌ها تنبل ساخته می‌شوند:

| فایل | کار |
|---|---|
| `blocked-domains.txt` | دامنه‌هایی که باید فقط از تونل بروند؛ خودکار یاد گرفته می‌شود و قابل ویرایش دستی است (بعد از ویرایش restart لازم است) |
| `state.json` | اولین چرخش تونل |
| `gateway.json` | اولین استارت xray |
| `tunnel.log` | اولین استارت xray |

همهٔ فایل‌های حاوی اعتبار (توکن، pool، gateway، state، لاگ) حالت ۰۶۰۰ دارند.

`pool.txt` را خودتان پر می‌کنید؛ بدون آن، جست‌وجو مستقیم انجام می‌شود و DDG از این
شبکه با کپچا شکست می‌خورد (با `error_code=captcha`، نه لیست خالی).

## اجرا

```bash
websearch serve                    # http://127.0.0.1:7971
websearch serve --port 7972
```

به‌عنوان سرویس کاربری:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/websearch.service ~/.config/systemd/user/
# اگر با venv نصب کرده‌اید، ExecStart را در unit به همان مسیر .venv/bin اصلاح کنید.
systemctl --user daemon-reload
systemctl --user enable --now websearch
journalctl --user -u websearch -f
```

## تنظیمات

ترتیب اولویت: `DEFAULT` در کد < `config.toml` < env با الگوی `WEBSEARCH_SECTION__KEY`.

```bash
WEBSEARCH_SERVER__PORT=8000
WEBSEARCH_SEARCH__CACHE_TTL=0
WEBSEARCH_EGRESS__MODE=tunnel        # auto | direct | tunnel
WEBSEARCH_TOKEN=...                  # جای فایل توکن
WEBSEARCH_HOME=/path/to/home         # همهٔ مسیرهای نسبی زیر این می‌روند
```

### مسیر خروج

`mode=auto` یعنی: دامنه در `blocked-domains.txt` هست؟ فقط تونل. نیست؟ اول مستقیم، و
فقط اگر شکست مسیر خورد (timeout/reset/DNS/403/451) یک‌بار تونل. اگر تونل جواب داد، دامنه
با تاریخ و دلیل به لیست اضافه می‌شود تا دفعهٔ بعد مستقیم تلاش نشود.

کپچا و صفحهٔ بلاک به‌طور خودکار مسیر را عوض نمی‌کنند؛ خطای بک‌اند هستند و breaker
را باز می‌کنند. اگر مسیر مستقیم کپچا می‌گیرد ولی تونل جواب می‌دهد (مثل DDG در این
شبکه)، دامنه را دستی در `blocked-domains.txt` بگذارید تا همیشه از تونل برود؛ بعد از
ویرایش، سرویس را restart کنید. این override موقتی است: تا وقتی در لیست باشد مسیر مستقیم
دیگر امتحان نمی‌شود، پس بعد از سالم‌شدن مسیر مستقیم خط را بردارید و restart کنید.
کنترل دستی:

```bash
websearch tunnel up|down|status
websearch status
```

## اتصال MCP

کلاینت باید URL و هدر توکن را بداند. نمونهٔ OpenCode v2 (تست‌شده در سشن واقعی):

```jsonc
{
  "mcp": {
    "servers": {
      "websearch": {
        "type": "remote",
        "oauth": false,
        "url": "http://127.0.0.1:7971/mcp",
        "headers": { "Authorization": "Bearer <token از ~/.config/websearch/token>" }
      }
    }
  }
}
```

`oauth: false` لازم است؛ وگرنه OpenCode برای سرور remote به‌طور پیش‌فرض OAuth را امتحان
می‌کند. سرورهای محلی (stdio) همچنان زیر کلید `mcp.servers` می‌آیند، نه مستقیم زیر `mcp`.

ابزار `search` سه پارامتر دارد: `query` (لازم)، `count` (پیش‌فرض ۱۰، سقف ۳۰)،
`lang` (`auto` پیش‌فرض؛ برای فارسی `fa`، انگلیسی `en`، یا کد منطقه‌ای DDG مثل `de-de`).

## نمونهٔ REST

```bash
TOKEN=$(cat ~/.config/websearch/token)
curl -s "http://127.0.0.1:7971/search?q=%D8%AA%D8%AD%D9%82%DB%8C%D9%82&count=5" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "query": "تحقیق", "lang": "fa", "count": 5, "partial": true, "cached": false,
  "error_code": null,
  "results": [{"title": "...", "url": "...", "snippet": "...", "engine": "mwmbl"}],
  "backends": [
    {"name": "ddg", "status": "error", "count": 0, "latency_ms": 800,
     "error_code": "captcha", "detail": "block page from the backend"},
    {"name": "mwmbl", "status": "ok", "count": 5, "latency_ms": 467}
  ]
}
```

- `200` = حداقل یک بک‌اند `ok` بود (حتی اگر `partial` باشد).
- `502` = هیچ بک‌اندی `ok` نبود؛ `backends[].error_code` دلیل را می‌گوید.
- `401` = توکن غایب/غلط؛ `/healthz` همیشه باز است.
- بک‌اند غیرفعال‌شده در کانفیگ با `status: "skipped"` می‌آید و `partial` را true نمی‌کند.

### کدهای خطا

`captcha`، `blocked`، `timeout`، `unreachable`، `http_error`، `parse_error`،
`rate_limited`، `circuit_open`، `proxy_unavailable`، `proxy_busy`،
`all_backends_failed`، `no_backends_enabled`.
قاعده: **هرگز لیست خالی بدون دلیل برنمی‌گردد.**

## آزمون

```bash
.venv/bin/python -m pytest -q
websearch doctor --live     # توکن، xray، pool، Mwmbl و باز شدن canary از تونل
```

پارسر DDG با دو fixture تست می‌شود چون شکننده‌ترین بخش است:
`tests/fixtures/ddg_results.html` (صفحهٔ واقعی DDG، ضبط ۱۴ نوامبر ۲۰۲۶) و
`ddg_results_synthetic.html` (حالت‌های لبه: فارسی، لینک ساده، entity). اگر ساختار HTML
عوض شد، اول fixture واقعی تازه ضبط کنید، بعد پارسر را.

## محدودیت‌های شناخته‌شده

- **DDG:** جست‌وجوی خودکار خلاف شرایط استفادهٔ DDG است و با نرخ بالا کپچا می‌دهد. برای
  همین `min_interval=2s`، یک درخواست هم‌زمان، و کش ۵ دقیقه پیش‌فرض است. کپچا یک وضعیت
  موقت است؛ breaker ۱۰ دقیقه باز می‌ماند و Mwmbl تنها کار می‌کند.
- **Mwmbl:** ایندکس کوچک و انگلیسی‌محور؛ برای کوئری فارسی معمولاً چیزی ندارد و
  `status=ok, count=0` برمی‌گرداند (نه خطا).
- **تونل:** کانفیگ‌های عمومی اشتراکی هر چند درخواست یک‌بار reset می‌شوند؛ چرخش خودکار
  هست، ولی اگر کل pool مرده باشد `proxy_unavailable` می‌گیرید و باید `pool.txt` را
  تازه کنید.
- **تحریم:** تونل فیلتر کشوری را حل می‌کند، نه ۴۰۳ تحریمی را. دامنه‌ای که هر دو مسیر
  ۴۰۳ بدهد `blocked` می‌شود و یاد گرفته نمی‌شود.
