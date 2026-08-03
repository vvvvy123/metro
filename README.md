# Metro Transfer

社区共同维护的全球地铁最佳换乘位置。全栈 MVP —— 数据层 + 后端 API + 前端。

本版重点修复了 V1 的核心问题：**线路 / 方向没有和城市绑定**（北京西直门 · 2号线 却出现上海方向）。
现在归属链是 `City → Line → Direction`、`City → Station`、`Station ⇄ Line`（多对多），
数据源头统一为 `data/<city>.json`，导入时强制校验城市↔线路↔方向的一致性。

## 目录

```
metro-transfer/
├── schema.sql          # SQLite 结构（修正后的关系模型）
├── data/               # 唯一数据源，一城一文件
│   ├── beijing.json
│   ├── shanghai.json
│   └── tokyo.json
├── data_format.md      # JSON 格式规范 + GTFS/OSM 数据源指引
├── import_city.py      # JSON → SQLite，含数据正确性校验（零依赖）
├── server.py           # 后端 API（Python 标准库，零依赖）
├── requirements.txt    # 说明：无需 pip install
└── web/
    └── index.html      # 前端（双模式：连后端走 API，否则离线预览）
```

## 双模式设计（因为你这台机器只有浏览器）

前端启动时探测 `http://localhost:8000/api/health`：

- **🟢 API 模式**：探测到后端 → 所有操作走真实 API，落库 `metro.db`。
- **🟡 离线预览模式**：探测不到 → 用页面内联的三城市种子数据（与 `data/*.json` 同源），
  投票 / 上传 / 评论 / 新增城市线路等写入 `localStorage`，刷新保留、`localStorage.clear()` 重置。

因此：**在能跑 Python 的机器上是完整全栈；在只有浏览器的机器上双击即可预览，交互完全一致。**

## 在能跑 Python 的机器上（完整全栈）

需要 Python 3.8+，**无需 pip install**（后端只用标准库）。

```bash
python import_city.py --all --reset     # 建库并导入全部城市（--city beijing 可单导）
python server.py                         # → http://localhost:8000
# 浏览器打开 web/index.html，顶部显示「🟢 已连接后端」即为全栈模式
```

> 让另一台机器（如只有浏览器的电脑）连上：把 `web/index.html` 里的 `API_BASE`
> 改成后端机器的 `http://<ip>:8000`（后端已监听 `0.0.0.0`）。

## 在只有浏览器的机器上（离线预览）

直接双击 `web/index.html`。顶部显示「🟡 离线预览模式」即可开始点。

## 新增一个城市（维护成本 ≈ 一分钟）

```
1. 写 data/<city>.json（手写，或由 GTFS/OSM 转换脚本生成，见 data_format.md）
2. python import_city.py --city <city>    # 自动校验城市↔线路↔方向一致性，脏关联会被拒绝
3. 重启 server.py
```

不建议爬各地铁官网（结构各异、难维护）。推荐 GTFS / OpenStreetMap 统一转换，详见 `data_format.md`。

## 身份（无登录）

已按需求**移除登录功能**。浏览 / 上传 / 点赞 / 点踩 / 评论都无需登录。
身份用匿名的**设备标识**：前端在 `localStorage` 生成一个 `X-Device-Id`，每次请求带上；
后端据此映射到一条 `user` 记录（`email` 列存 `device:<id>`），从而在无账号的情况下仍能实现
「同一换乘每人一条回答」「一票（可取消/切换）」。匿名开关只影响**是否对外显示昵称**。

> 若将来要恢复真实账号，只需把 `_uid_ensure` 换成基于登录 token 的用户解析即可，其余逻辑不变。

## 对照 PRD 与反馈的实现清单

- ✅ 城市：可搜索（中文/拼音/英文/别名）、可新增（不再写死三城市）
- ✅ 线路 / 方向严格绑定城市 —— 修复串城市 bug（import 阶段强制校验）
- ✅ 上传：Step 向导（城市→车站→线路方向→位置→确认）；每个字段「搜索 + 没有则新增」
- ✅ 搜索：中文 / 拼音 / 首字母 / 英文 / 模糊
- ✅ 首页：最近搜索（localStorage）+ 热门车站
- ✅ 无登录：匿名设备标识身份（已按第二版反馈移除登录）
- ✅ 回答：同一换乘每人一条，再次发布 → 新版本、刷新排序时间
- ✅ 排序：Score = 点赞率 × 热度 × 时间衰减，差距 <3% 时新回答优先
- ✅ 点赞/点踩（一票、可取消可切换）、一级评论、匿名（仅隐藏昵称）、删除（软删，历史保留）
- ✅ 按 PRD 5.3：**不显示「高可信/中可信」标识**（旧版有，本版已移除）

## 下一步建议

- `gtfs_to_json.py`：把 GTFS/OSM 原始数据批量转成本规范，规模化补数据。
- 迁移 `server.py` 到 FastAPI（更易扩展），届时启用 `requirements.txt`。
- 数据层稳定后再做 V2：收藏、最近使用、贡献排行榜。
