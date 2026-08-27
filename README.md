# QoderCN Tools

一个独立的服务，把 QoderCN 网关（`gateway.qoder.com.cn`）自带的工具重新暴露出来：三个 JSON 接口（webSearch / imageSearch / imageGen）、一个流式语音识别 WebSocket（asr）、一个文本润色透传接口（polish）。

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

流式语音识别，代理到网关的 `fun-asr-realtime`（阿里 FunASR，免费）。这是一个 **WebSocket**：客户端流式发送音频，服务端注入 COSY 签名后转发到上游。

- **音频格式**：`SampleRate`（**必须等于音频实际采样率**，否则转写乱码）、`Channels`、`BitDepth`、`FrameDurationMs`，以及可选 `Accept-Language`。
- **发送**：16-bit 小端裸 PCM 二进制帧（采样率/声道按你声明的头）；结束时发文本帧 `{"type":"voice_completed","message":"close by user"}`。
- **接收**（网关下发的 JSON 文本帧）：
  - `{"type":"speech_delta","message":"<累积文本>","model_name":"fun-asr-realtime",...}` —— 中间结果（partial）
  - `{"type":"speech_completed","message":"<整句>",...}` —— 分句定稿（已带标点、大小写）
  - `{"type":"speech_done","status":200}` —— 全部结束
  - `{"type":"speech_err","code":...,"message":...}` —— 出错

### `POST /polish` → `voice/polish`

文本润色（给口述/ASR 文本加标点、规范大小写、英文顺手删口头禅与重复词；不改措辞、不翻译、不作答；免费）。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `text` | string | （必填） | 要润色的原文（口述/ASR 文本） |

服务端自动用 `<transcription>` 包裹、生成 session/request id（`client_type` 固定 `"5"`）。返回上游原样 JSON：`{result:{content:"润色后文本"}, success, traceId}`。

示例：`嗯那个我们明天上午十点开会` → `嗯，那个，我们明天上午十点开会。`；`so um i think we should uh refactor` → `So I think we should refactor.`

### `POST /v1/audio/transcriptions` → `ws/asr`（OpenAI 兼容）

OpenAI 音频转写 API 的兼容实现（`multipart/form-data` 上传），把上传的音频解码后经网关 ASR（`fun-asr-realtime`）批量转写。可直接用 OpenAI SDK：把 `base_url` 指向本服务的 `/v1`、`api_key` 用本服务的 key 即可（`Authorization: Bearer` 会被识别）。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | 文件 | （必填） | 任意常见格式（mp3/m4a/webm/wav/…），内部用 PyAV 解码重采样为 16k 单声道 16-bit。上限 25MB |
| `model` | string | `fun-asr-realtime` | 仅为兼容而接收；网关模型固定，忽略 |
| `language` | string | 无 | 转成网关的 `Accept-Language` |
| `response_format` | string | `json` | `json`（`{"text":...}`）/ `text` / `verbose_json` / `srt` / `vtt` |
| `stream_realtime` | bool | 见 `ASR_REALTIME_PACING` | 本次是否按 1x 实时节奏推流（影响字幕时间戳精度，见下） |

`prompt`/`temperature`/`timestamp_granularities` 为兼容而接收但忽略（网关无对应能力）。

字幕时间戳（`srt`/`vtt`/`verbose_json` 的分段）：优先用网关按句返回的时间戳；网关不给时，**开启节奏**（`stream_realtime=true` 或 `ASR_REALTIME_PACING=true`）则用推流时间轴，否则按各句字数在总时长上按比例分配（较粗）。`json`/`text` 的文本内容不受时间戳影响，始终准确。


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
| `IMAGEGEN_URL` / `WEBSEARCH_URL` / `IMAGESEARCH_URL` / `ASR_URL` / `POLISH_URL` / `OPENAI_TRANSCRIPTIONS_URL` | 各工具的路由路径（`ASR_URL` 是 WebSocket，`POLISH_URL` 是原样透传 POST，`OPENAI_TRANSCRIPTIONS_URL` 是 OpenAI 兼容的 multipart 转写 POST，其余是 POST）。**不设置 ⇒ 不暴露该工具。** 全部不设置 / 路径非法 / 互相重合 ⇒ 启动失败。 |
| `ASR_REALTIME_PACING` | `true`/`false`（默认 `false`）—— 转写时的默认推流节奏；仅在网关不返回时间戳时影响 `srt`/`vtt`/`verbose_json` 的分段时间精度。可被每次请求的 `stream_realtime` 覆盖。 |
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
