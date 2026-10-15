# WorkBuddy 开发上下文

本文件用于核验「麦麦最优解」项目使用腾讯 WorkBuddy 开发的真实性。

## 开发方式

全程在 WorkBuddy（Craft 模式）中通过对话完成：创意选型 → MCP 接口探测 → 编码 → 真实数据联调 → 文档撰写。
WorkBuddy 负责读取本地 MCP 配置、发起接口调用验证返回结构、迭代修正约束模型并生成参赛文档。

## 关键对话节点

1. **赛事规则分析**：WorkBuddy 拉取活动仓库与 `activityGuidelines.md`，确认排名依据为 GitHub Star 数，
   并整理出必须提交的 5 个文件清单。
2. **创意选型**：基于 MCP 的 35 个 Tool 能力，产出 8 个候选创意的评分矩阵
   （痛点强度 / 创意独特性 / Star 潜力 / 工具覆盖 / 落地速度），最终选定
   「省钱 + 营养」双约束求解方向。
3. **接口探测**：WorkBuddy 直接调用 `tools/list` 拿到 35 个 Tool 的真实 schema，
   并逐一实测 `query-nearby-stores`、`query-meals`、`list-nutrition-foods`、
   `query-meal-detail`、`calculate-price`、`available-coupons` 的返回结构，
   据此编写解析逻辑，避免凭猜测写代码。
4. **联调迭代**（两轮关键修正）：
   - 第一轮求解结果为「圆筒冰淇淋 ×2」，原因是候选池只按价格排序、缺少主食与热量下限约束；
     WorkBuddy 引入主食件数下限、热量下限与分层采样后修正。
   - 第二轮发现 104 个餐品仅 33 个能匹配到营养数据；WorkBuddy 增加
     `query-meal-detail` 套餐组成反推，显著提升覆盖率。
5. **文档生成**：WorkBuddy 依据真实运行输出撰写 README.md、MCP_INTEGRATION.md 与 SKILL.md。

## 真实运行记录

```
$ python3 scripts/optimizer.py --city 上海 --keyword 人民广场 --people 2 --budget 70 --kcal 800
门店：麦当劳上海黄浦华旭国际大厦餐厅（1450713）｜2 人｜预算 ¥70｜人均热量上限 800 kcal
【最省钱】那么大鸡排（椒盐风味）×1 + 麦乐鸡套餐×1
  实付 ¥39.00（菜单价 ¥39.00）｜人均 ¥19.50
  热量 1034 kcal（人均 517）｜蛋白 40 g｜钠 1583 mg
【最扛饿】麦乐鸡套餐×1 + 酥脆无双套餐×1
  实付 ¥45.00｜人均 ¥22.50｜热量 1083 kcal｜蛋白 39 g｜钠 1588 mg
【最均衡】麦辣鸡腿汉堡×1 + 巨无霸×1
  实付 ¥48.00｜人均 ¥24.00｜热量 998 kcal｜蛋白 51 g｜钠 2169 mg
```

以上结果于 2026-10-15 通过麦当劳 MCP 实时接口获取。
