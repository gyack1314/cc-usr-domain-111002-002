# 合同—履约批次—凭证层级采集器（Playwright）

通过**真实浏览器的可见页面操作**，从门户合同列表进入嵌套页面，整理
「合同 → 履约批次 → 批次凭证」三层归属关系，并为每条归属保留可复查证据。
采集端不读取门户内部状态，不调用 `/__control`、`/__state`、`/api/runtime`，
也不直接读 `data.json`（测试对这些字符串做静态零引用约束）。

## 页面层级与作用域

```
主页面（合同列表 #contracts）
└ iframe#detail  合同详情（切换合同时整个 iframe 被替换重建）
   ├ 合同层“下载”按钮（body > button）
   └ iframe#batch  履约批次（切换批次时整个 iframe 被替换重建）
      ├ 批次文档层“下载”按钮（body > button）
      └ receipt-panel（开放 Shadow DOM）
         └ section[data-owner-id][data-ready]
            ├ “编号”文本、凭证 data-receipt / data-digest
            └ Shadow 内“下载”按钮
```

不同层都有同名“编号/下载”控件。所有选择器都限定**当前层级 + 当前父记录**：
合同按钮限定 `#contracts [data-contract="…"]`，批次按钮限定详情 frame 的
`#batches [data-batch="…"]`，凭证读取限定当前批次 Shadow section，
绝不使用“页面第一个同名按钮”。

## 防串资料的核心规则

- **双匹配才读取**：每个 frame 的 `data-owner-id` 与 `data-ready` 必须同时
  匹配当前父项且为终态（`true`/`error`）。旧文本仍可见但新请求未完成
  （`ready` 非终态）时继续等待，不读取。
- **frame/面板重建即重解析**：每次重试都重新获取 `frame_locator` 与 locator，
  不缓存失效元素句柄；归属不匹配时丢弃本轮重新解析。
- **空 ≠ 失败**：合同下没有批次记为合同级 `empty=true` 的正常完成；
  批次 `data-ready=error` 记为该批次 `status=error`，失败挂在对应父批次下，
  绝不把空数组当成功。
- **共享凭证保留两条关系**：凭证内容按凭证编号只存一份，批次→凭证的引用
  关系独立保存；同一凭证被两个批次引用会得到两条 `receipt_refs`。
- **同号不同摘要保留冲突**：相同 `receipt_id` 出现不同 `digest` 时，在
  `receipts[id].variants` 下并存，不就近覆盖。
- **单页单链**：一个浏览器页面只处理一条合同父级链；页面在链未完成时关闭，
  该链标记 `interrupted`（未收齐），已读到的批次保留，绝不标 `complete`。
- **幂等续采**：重跑时已 `complete` 的合同跳过；`interrupted` 链允许重采，
  已确认的合同/批次关系按稳定编号合并，引用关系不会翻倍。

## 运行前提

需要 Python 3.11+ 与 Playwright，以及一个可用的 Chromium。本机若无法访问
Playwright 浏览器 CDN，可通过 `CHROME_PATH` 指向任意 Chromium 可执行文件，
采集器以 `executable_path` 驱动它。参考仓库根 `chrome-env.sh`（本机离线
Chromium 的库、字体、Vulkan 环境变量）。

## 从列表出发的采集命令

先启动门户：

```sh
python -m portal --port 8090
```

再从合同列表采集（`-I` 隔离模式下用根目录启动器，它会显式登记工程路径）：

```sh
python -I run_collector.py \
  --base-url http://127.0.0.1:8090 \
  --out ./collected/run1 \
  --run run-001 \
  --concurrency 2
# 只采部分合同：--only CT-17 CT-29
# 有头调试：--headed
# 指定浏览器：--executable-path /path/to/chrome（或设 CHROME_PATH）
```

退出码：`0` 全部收齐；`1` 存在未收齐（可重采）链；`2` 发生采集异常。

## 层级结果（result.json）

- `contracts[]`：每条合同的状态、详情 frame 地址、owner/ready 证据、
  批次编号列表、是否空、下属 `batches[]`（含批次 frame 地址、Shadow 定位、
  凭证编号/摘要、同名控件计数、失败信息）与整页截图路径。
- `receipt_refs[]`：批次→凭证引用关系，含合同、批次、凭证编号、摘要、
  frame 地址与证据。共享凭证在此出现多次。
- `receipts{}`：按凭证编号去重的凭证内容；`variants` 保存同号不同摘要冲突。
- `screenshots/`：合同整页截图与失败批次截图，作为归属复查证据。

据此可查到每份凭证来自哪个合同、哪个批次、哪一级 frame 地址以及当时的
页面 owner/ready 状态。

## 测试

```sh
python -I run_tests.py tests.test_portal     # 门户自身行为（7 项）
python -I run_tests.py tests.test_collector  # 采集器真实浏览器端到端（10 项）
```

采集器测试覆盖：全量层级与共享凭证、空批次 vs 失败批次、快速切换合同/批次
不串资料、页面中途关闭后的 interrupted 与幂等续采、同号摘要冲突、同名控件
作用域限定、只经可见页面操作、CLI 端到端。
