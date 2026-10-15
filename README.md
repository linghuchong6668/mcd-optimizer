# 麦麦最优解 · Mcd Optimizer

> 在「预算 × 热量 × 人数 × 忌口」多重约束下，求解麦当劳最优下单方案的 Skill。
> 数据全部来自麦当劳 MCP Server 的真实接口，不做任何硬编码菜单。

减肥期想吃麦当劳、两个人只有 60 块、团队下午茶要凑够 8 份还不能太咸——
这些本质上是同一个问题：**带约束的组合优化**。这个项目把它解出来，并给出三套方案：

| 目标 | 含义 |
| --- | --- |
| 最省钱 | 满足热量下限前提下的最低实付，附人均花费 |
| 最扛饿 | 每元买到的千卡最高（性价比） |
| 最均衡 | 蛋白供能比接近 20%、钠更低、总热量贴近目标 |

## 为什么不是"随便挑几样"

1. **真实数据**：门店、菜单、现价/原价、营养成分全部由 MCP 实时返回，菜单换了项目不用改。
2. **套餐营养反推**：营养表里查不到的「XX三件套」，用 `query-meal-detail` 拆出默认组成（主食+小食+饮料）后逐项累加，营养覆盖率从 32% 提到接近全覆盖。
3. **约束建模**：主食件数、预算上限、热量上下限、忌口关键词同时生效，避免"最省钱=两个冰淇淋"这种废解。
4. **真实核价**：本地枚举只解决搜索空间，最终价格一律回调 `calculate-price` 核价，拿到真实实付与优惠额。

## 安装

纯 Python 标准库，无需 pip 安装任何依赖。

```bash
git clone https://github.com/<你的账号>/mcd-optimizer.git
cd mcd-optimizer
```

准备麦当劳 MCP Token（获取方式见 [麦当劳 MCP Server](https://github.com/M-China/mcd-mcp-server)）：

```bash
export MCD_MCP_TOKEN=<你的 Token>
```

把 MCP 接入任意支持 Streamable HTTP 的客户端（示例见 `mcp-config.example.json`）。

## 使用示例

```bash
python3 scripts/optimizer.py \
  --city 上海 --keyword 人民广场 \
  --people 2 --budget 70 --kcal 800 --top 1
```

真实输出（上海黄浦某门店，2026-10-15 拉取）：

```
门店：麦当劳上海黄浦华旭国际大厦餐厅（1450713）｜2 人｜预算 ¥70｜人均热量上限 800 kcal
======================================================
【最省钱】那么大鸡排（椒盐风味）×1 + 麦乐鸡套餐×1
  实付 ¥39.00（原价 ¥39.00）｜人均 ¥19.50
  热量 1034 kcal（人均 517）｜蛋白 40 g｜钠 1583 mg
    - 那么大鸡排（椒盐风味） ×1  ¥14.00  385 kcal
    - 麦乐鸡套餐 ×1  ¥25.00  649 kcal
【最扛饿】麦乐鸡套餐×1 + 麦香鸡×1
  实付 ¥42.00（原价 ¥42.00）｜人均 ¥21.00
  热量 1018 kcal（人均 509）｜蛋白 31 g｜钠 1318 mg
    - 麦乐鸡套餐 ×1  ¥25.00  649 kcal
    - 麦香鸡 ×1  ¥17.00  369 kcal
【最均衡】麦辣鸡腿汉堡×1 + 巨无霸×1
  实付 ¥48.00（原价 ¥48.00）｜人均 ¥24.00
  热量 998 kcal（人均 499）｜蛋白 51 g｜钠 2169 mg
    - 麦辣鸡腿汉堡 ×1  ¥22.50  485 kcal
    - 巨无霸 ×1  ¥25.50  513 kcal
```

更多场景：

```bash
# 减脂期单人餐：热量封顶 500 kcal，必须有主食
python3 scripts/optimizer.py --people 1 --budget 30 --kcal 500 --main 1

# 4 人加班餐，忌辣
python3 scripts/optimizer.py --people 4 --budget 150 --kcal 900 --avoid 辣

# 输出 JSON，方便接自己的前端
python3 scripts/optimizer.py --people 2 --budget 60 --json
```

## 目标用户

- 想控卡又想吃麦当劳的人（减脂 / 健身 / 体检后忌口）
- 精打细算的学生、上班族（同样花钱，吃得更饱）
- 帮团队订下午茶、加班餐的组长 / 行政（按人头与预算自动配平）

## 项目结构

```
.
├── SKILL.md                 # WorkBuddy Skill 定义与使用说明
├── README.md
├── MCP_INTEGRATION.md       # MCP 调用流程与业务价值
├── CONTEST_DECLARATION.md   # 参赛声明（官方原文，未改动）
├── mcp-config.example.json  # 脱敏 MCP 配置示例
├── workbuddy.md             # WorkBuddy 开发上下文
└── scripts/
    ├── mcd_mcp.py           # MCP Streamable HTTP 客户端（缓存 + 限流退避）
    ├── nutrition.py         # 营养表解析与餐品名模糊匹配
    └── optimizer.py         # 约束建模、组合枚举、核价与排序
```

## 免责声明

本项目为麦当劳程序员节创意开发大赛参赛作品，由参赛者独立开发，非麦当劳官方产品。
输出仅供参考，不构成医疗、营养或其他专业建议；餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。
