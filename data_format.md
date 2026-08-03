# Metro Transfer — 统一数据格式规范

一个城市 = 一个 JSON 文件（`data/<city_id>.json`）。这是**唯一数据源**：
`import_city.py` 读它写入 SQLite，前端离线模式也读同一批文件。
新增一个城市 = 新增一个 JSON 文件，不改任何代码。

## 为什么这样设计

V1 最大的 bug：线路和方向没有和城市绑定，导致「北京西直门 · 2号线」出现上海方向（往浦东）。
根因是线路只按名字（"2号线"）全局索引，而北京和上海都有 2号线。

修正后的归属链（见 `schema.sql`）：

```
Country → City → MetroSystem → Line → Direction
                        └────→ Station ──(station_line 多对多)──→ Line
Transfer(station, from_line, from_dir, to_line, to_dir) → Answer → Version / Vote / Comment
```

- `line.city_id` 强约束：线路属于城市。
- `direction.line_id` 强约束：方向属于线路（因此也属于城市）。
- `station_line` 多对多：车站只能关联**本城市**的线路。

结果：搜「西直门」只会出现北京的线路；选北京 2号线只会出现「内环 / 外环」，永远不会出现「往浦东」。

## 文件结构

```jsonc
{
  "city": {
    "id": "beijing",              // 全局唯一，小写，用作主键与文件名
    "country_id": "cn",
    "country_cn": "中国",
    "country_en": "China",
    "name_cn": "北京",
    "name_en": "Beijing",
    "alias": ["BJ", "beijing", "bj", "Peking"],  // 全部可搜索
    "timezone": "Asia/Shanghai"
  },

  "system": {                      // 可选：运营方 / 系统
    "id": "beijing-subway",
    "name_cn": "北京地铁",
    "name_en": "Beijing Subway"
  },

  "lines": [
    {
      "id": "bj-l2",               // 全局唯一（建议 <city 前缀>-<line>）
      "name": "2号线",
      "name_en": "Line 2",
      "color": "#006098",          // 用于线路色块
      "directions": ["内环", "外环"]   // 方向属于本线路
    }
  ],

  "stations": [
    {
      "id": "bj-xizhimen",
      "name_cn": "西直门",
      "name_en": "Xizhimen",
      "alias": ["xizhimen", "xzm"],   // 拼音 / 首字母 / 英文，全部可搜
      "lines": ["bj-l2", "bj-l4", "bj-l13"]   // 只能引用本文件里的线路 id
    }
  ],

  "seed_answers": [                // 可选：示例回答，方便演示与冒烟测试
    {
      "station": "bj-xizhimen",
      "from_line": "bj-l2", "from_dir": "内环",
      "to_line": "bj-l13", "to_dir": "往东直门",
      "author_email": "laowang@example.com",
      "author_nick": "通勤老王",
      "anon": true,
      "position_type": "custom",   // "car" | "custom"
      "car_number": null,          // position_type=car 时填
      "custom_text": "车尾第1节",   // position_type=custom 时填
      "description": "……",
      "likes": 42, "dislikes": 5,
      "version": 2,
      "days_ago": 7,               // 相对导入当天的天数，用于时间衰减演示
      "comments": [
        { "nick": "匿名", "days_ago": 5, "text": "扶梯维修中" }
      ]
    }
  ]
}
```

## 字段校验规则（import_city.py 会强制）

1. `station.lines` 里的每个 id 必须在本文件 `lines` 中存在 → 否则报错（避免脏关联）。
2. `seed_answers` 的 `from_dir` / `to_dir` 必须是对应线路 `directions` 里的值。
3. `from_line`、`to_line` 必须出现在该 `station.lines` 中。
4. `id` 建议带城市前缀，保证全局唯一，跨城市不冲突。

任何一条不满足，导入会中止并指出问题所在——这正是 V1 缺的“数据正确性守门”。

## 数据初始化：不要爬官网

各官网结构不同、维护成本高。推荐按优先级使用成熟开放数据，统一转成上面的 JSON：

| 优先级 | 数据源 | 覆盖 | 说明 |
|---|---|---|---|
| ★★★★★ | **GTFS** | 全球 | 许多城市官方发布 `stops.txt` / `routes.txt` / `trips.txt`，含站点、线路、方向。 |
| ★★★★★ | **OpenStreetMap** | 全球 | `route=subway` relation，Overpass API 可批量拉取。 |
| ★★★☆☆ | 城市开放数据平台 | 局部 | 北京 / 上海 / 香港 MTR 等官方开放接口。 |
| ★☆☆☆☆ | 官网 | 兜底 | 最后手段，不建议直接爬。 |

建议提供转换脚本（后续）：`gtfs_to_json.py --gtfs beijing_gtfs/ --out data/beijing.json`，
把 GTFS/OSM 原始数据映射为本规范，人工 review 后入库。这样维护成本从“逐站手填”降到“跑一次脚本 + 校对”。

## 加一个城市的完整流程

```
1. 准备 data/<city>.json（手写，或由 GTFS/OSM 转换生成）
2. python import_city.py --city <city>     # 校验 + 写入 SQLite
3. 重启 server.py（或热加载）— 前端立即可搜到新城市
```
