# QoderCN Tools

一个独立的服务，把 QoderCN 网关（`gateway.qoder.com.cn`）自带的工具重新暴露出来：三个 JSON 接口（webSearch / imageSearch / imageGen）加一个流式语音识别 WebSocket（asr）。请求参数直接对应上游，服务内部只负责 COSY 签名。

### `POST /webSearch` → `oneSearch`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | （必填） | 搜索词 |
| `timeRange` | string | `NoLimit` | 时间过滤，仅认 `NoLimit`/`OneDay`/`OneWeek`/`OneMonth`/`OneYear`（近1天/周/月/年） |
| `contents.mainText` | bool | `false` | 每条结果的完整正文抽取（约 1.6KB/条，全部结果） |
| `contents.markdownText` | bool | `false` | markdown 版正文（尽力而为，只有部分页面能抽出） |
| `contents.summary` | bool | `true` | AI 合成的总结（约 0.55KB/条，几乎每条都有） |

返回上游原样 JSON：`{pageItems:[{title, link, snippet, summary, mainText, markdownText, publishedTime, hostname, hostLogo}], searchInformation:{searchTime}, requestId}`（各字段按上面的开关填充）。

### `POST /imageSearch` → `imageSearch`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | （必填） | 图片搜索词 |
| `count` | int (1–10) | `5` | 返回图片数 |

返回：`{results:[{title, imageUrl, width, height}], success, count, query}`。

### `POST /imageGen` → `generateImage`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | （必填） | 图片描述 |
| `size` | string | `1024x1024` | 十档宽高比之一（见下） |
| `model` | string | `qmodel_38max` | 生图模型 |

`size` 可选值（宽高比）：`1024x1024`(1:1)、`1536x1024`(3:2)、`1024x1536`(2:3)、`768x1024`(3:4)、`1024x768`(4:3)、`1024x1280`(4:5)、`1280x1024`(5:4)、`1024x1792`(9:16)、`1792x1024`(16:9)、`2560x1080`(21:9)。为向前兼容不做硬校验，非法值由上游报错。返回：`{created, data:[{url: data-url}], usage}`。

### `WS /asr` → `ws/asr`

流式语音识别，透传代理到网关的 `fun-asr-realtime`（阿里 FunASR，免费）。与上面三个不同，这是一个 **WebSocket**：客户端的握手头、音频帧、控制帧都原样转发到上游，服务内部只注入 COSY 签名。目前是**纯透传**——客户端需自行按下面的协议发送。

握手头（客户端提供，原样透传）：

| 头 | 值 | 说明 |
|---|---|---|
| `SampleRate` | `16000` | 采样率 |
| `FrameDurationMs` | `100` | 帧长（毫秒） |
| `BitDepth` | `16` | 位深 |
| `Channels` | `1` | 单声道 |
| `X-Asr-Session-Id` | `<uuid>` | 本次会话 ID |
| `X-Business` | `{"product":"ide","type":"asr_chat","id":<uuid>,"begin_at":<ms>,"name":"asr_chat-<uuid>"}` | 业务信封 |
| `Accept-Language` | 如 `en-US` | 可选，识别语言 |

- **发送**：原始 **16kHz 单声道 16-bit 小端 PCM** 二进制帧；结束时发一个文本帧 `{"type":"voice_completed","message":"close by user"}`。
- **接收**（网关下发的 JSON 文本帧）：
  - `{"type":"speech_delta","message":"<累积文本>", "model_name":"fun-asr-realtime", ...}` —— 中间结果（partial）
  - `{"type":"speech_completed","message":"<整句>", ...}` —— 分句定稿（已带标点、大小写）
  - `{"type":"speech_done","status":200}` —— 全部结束
  - `{"type":"speech_err","code":...,"message":...}` —— 出错

> 后续计划：加一个 OpenAI 兼容的 `/v1/audio/transcriptions`（上传音频文件 → 内部转 16k PCM → 走本 WS → 聚合返回 `{"text"}`），让普通 OpenAI 客户端零门槛调用。

是否暴露某个接口、路由路径、鉴权、图像处理都由 `.env` 控制。

## 安装

```bash
uv sync
```

## 运行

配置由项目根目录的 `.env` 文件驱动。复制示例后编辑；真实环境变量优先级高于 `.env`。

```bash
cp .env.example .env                 # 按需编辑
echo "my-secret-key" > auth-keys.txt # 创建 API_KEY_FILE 指向的密钥文件（一行一个 key，# 为注释）

uv run qodercn-tools                 # 读取 ./.env
uv run qodercn-tools --env-file /path/to/other.env
```

上游网关凭据会自动从 QoderCN IDE/CLI 登录缓存解密获取（也可用 `QODERCN_AUTH_FILE` 指定明文凭据文件）。

启动时会打印一份摘要（IP / 端口 / 已暴露服务 / 鉴权状态 / 图像开关），并在**绑定到公网地址且鉴权关闭**时给出 WARNING。

## 配置项（`.env`）

| 变量 | 含义 |
|---|---|
| `API_KEY_FILE` | 密钥文件路径（相对项目根，或绝对路径），一行一个 key，`#` 为注释。**留空 ⇒ 不鉴权。** key 只允许 `A-Za-z0-9_-@+=&*` 且长度 ≤ 50；出现其他字符、超长 key、或无有效 key ⇒ 启动失败。 |
| `RM_EXIF_INFO` | `true`/`false` —— 剥离生成图片中的 AIGC 追踪元数据（无损）。 |
| `RM_BLIND_WM` | `true`/`false` —— 破坏隐藏盲水印的载荷（几何去同步 + 有损重编码，会轻微改动像素，且不一定生效）。 |
| `IMAGEGEN_URL` / `WEBSEARCH_URL` / `IMAGESEARCH_URL` / `ASR_URL` | 各工具的路由路径（`ASR_URL` 是 WebSocket，其余是 POST）。**不设置 ⇒ 不暴露该工具。** 全部不设置 / 路径非法 / 互相重合 ⇒ 启动失败。 |
| `IP` | 绑定地址 / 暴露面（`127.0.0.1` 仅本机，`0.0.0.0` 所有网卡）。 |
| `PORT` | 监听端口。`-1` ⇒ 自动挑一个空闲端口；端口被占用 ⇒ 启动失败。 |

进阶（可选）：`QODERCN_BASE_URL`、`QODERCN_AUTH_FILE`、`LINGMA_CACHE_DIR`、`QODERCN_UPSTREAM_PROXY`、`QODERCN_COSY_VERSION`、`QODERCN_TIMEOUT` —— 详见 `.env.example`。

鉴权：请求头带 `x-api-key: <key>` 或 `Authorization: Bearer <key>`；WebSocket（`/asr`）还可用 query `?api_key=<key>` 或 `?token=<key>`。

`QODERCN_AUTH_FILE` 指向的明文凭据文件格式：

```json
{
  "source": "manual",
  "token_expire_time": "0",
  "auth": {
    "cosy_key": "...",
    "encrypt_user_info": "...",
    "user_id": "...",
    "machine_id": "...",
    "access_token": ""
  }
}
```

## 示例

```bash
curl -s http://127.0.0.1:8790/webSearch \
  -H "x-api-key: my-secret-key" -H "content-type: application/json" \
  -d '{"query": "谁发明了 Go 语言"}'

curl -s http://127.0.0.1:8790/imageSearch \
  -H "x-api-key: my-secret-key" -H "content-type: application/json" \
  -d '{"query": "corgi", "count": 3}'

curl -s http://127.0.0.1:8790/imageGen \
  -H "x-api-key: my-secret-key" -H "content-type: application/json" \
  -d '{"prompt": "桌上的一个红苹果"}'
```

## 关于生成图片的水印

网关生成的图片会内嵌一个 `AIGC` 追踪块（服务商统一社会信用代码 + 每张图的追踪 ID），以及一个右下角的可见水印。本服务默认（`RM_EXIF_INFO=true`）剥离 PNG 元数据块；可见水印是像素、无法无损去除。`RM_BLIND_WM=true` 时会额外破坏不可见盲水印的载荷（裁边 → 有损 JPEG → 双线性放大回原尺寸 → PNG）。
