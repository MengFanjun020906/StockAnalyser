# Agent 后验复盘入口恢复开发、测试与验收规格

本文面向执行 AI，收尾 `/agent-verdict-reviews` 页面入口迁移。后端 API/服务/测试仍保留，目标是让用户重新访问同一套 Trace + 本地行情后验复盘数据，而非把 Signal Preview 的影子评估改名复用。

## 当前事实与数据口径

- `api/v1/endpoints/agent_verdict_reviews.py` 提供 GET 查询和 POST rebuild；`AgentVerdictReviewService` 从本地 Trace 与本地 `StockDaily` 生成方向判断后验样本，支持 `stock_selection`、`single_stock_analysis`、标签、symbol 和 7/30 日等窗口。
- Web `App.tsx` 当前将 `/agent-verdict-reviews` 重定向到 `/signal-preview`，旧复盘页面/组件可能已在工作区删除（本仓库当前有未提交改动，执行时先确认文件现状与用户改动，不得恢复已删除旧文件）。
- 信号页影子评估与 verdict review 的样本来源、标签和统计口径不同。不能混算或以同一图表冒充一套数据。

## 产品目标与界面

恢复独立可访问的 Agent 后验复盘页，并在导航中提供清晰入口；保留 `/agent-verdict-reviews` 稳定 URL。页面至少有链路筛选、标签筛选、股票代码筛选、评估窗口说明、样本总数/完成数/完成率、收益与方向标签分布、结果表、加载/空/错误状态，以及显式“重建样本”操作。

重建按钮必须说明这是从本地 Trace/本地行情重建，不重跑 Agent、不拉外网行情；展示扫描 Trace 数、输出样本数、完成数和错误/跳过数。避免把尚未成熟窗口的 `insufficient_data` 计入已完成收益均值。页面应区分 pending/partial/completed 数据。

## 实现顺序

1. 检查当前分支/工作区中的旧 page/API client/nav 是否存在或被有意删除；尊重既有删除。根据当前 Web 架构新建最小独立页面和 typed API client，不从 Git 中恢复整个旧实现。
2. 确认 FastAPI router 仍挂载 `/api/v1/agent-verdict-reviews`，端点 schema/筛选参数及 rebuild 错误响应；补 API spec。
3. 读取服务返回体和实际 JSONL 样本结构，定义前端类型，不能依赖未承诺字段或硬编码统计标签文案；对未知标签提供安全 fallback。
4. 将 App route 从 Navigate 替换为页面，并恢复导航入口。保留旧拼写别名 `/agent-verdict-reviewsz` 的兼容重定向到 canonical page，若确认该 alias 仅为历史 typo 可做后续清理，不阻断首期。
5. 页面调用只读 GET；重建通过 POST，成功后刷新 GET。防止重复点击和并发 rebuild，显示服务错误，不吞异常。默认筛选由服务端口径决定，前端不得自行重分类样本。
6. 完成 API/前端测试和文档。后验结果保持离线只读，不注入 Agent prompt、Judge、候选权重或 Risk Gate。

## 测试验证

后端回归：运行现有 `tests/test_agent_verdict_review_service.py`，并对 GET 筛选、POST rebuild、非法参数、空样本、文件缺失/损坏、权限配置（若当前 API 要求认证）做契约测试。核对 router 实际路径。

前端：API client query 参数序列化、空/加载/错误/部分/完成状态、筛选组合、未知标签、重建成功后刷新、重建失败提示、按钮 loading/防重复。App route 测试确认 `/agent-verdict-reviews` 渲染复盘页且不跳到 `/signal-preview`；旧 alias 行为也覆盖。若已有 Playwright smoke，则加一条入口导航验证。

运行定向 pytest、Web `rtk npm run lint` / `rtk npm run build`，并执行 `rtk ./scripts/ci_gate.sh`。测试数据应明确标识样本源，防止 fixture 将 signal shadow data 当作 verdict review。

## 验收条件

- 用户可从导航和直接 URL 访问独立 verdict review 页面。
- GET 使用现有 AgentVerdictReviewService 数据，POST rebuild 使用现有服务并完成页面刷新。
- 统计口径与 Signal Preview 分开显示，不合并样本/标签/收益。
- 空、未成熟、部分数据和异常清楚可辨；页面不声称外网实时行情或实时策略效果。
- 不把复盘结果自动反馈至线上 Agent。

## 交付与回滚

用户可见路由和导航变化需更新 CHANGELOG、API spec（若 API 契约变更）与前端导航专题文档。回滚只需恢复 route/nav 到原重定向；保留 API、JSONL 和历史样本。若已有旧组件被用户删除，不为满足旧文件路径而复原其整套代码。
