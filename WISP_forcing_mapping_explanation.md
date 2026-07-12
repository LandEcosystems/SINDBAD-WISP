# SINDBAD forcing 与 WISP 输入变量映射：完整判定说明

## 1. 文档目的

本文档解释 [WISP_variable_table.md](WISP_variable_table.md) 中每一个 SINDBAD forcing 为什么被标记为 `Yes`、`Approximate` 或 `No`。这里回答的是：**SINDBAD 所需的物理量，在 WISP 当前正式输入中是否存在，以及存在到什么程度。**

本文档不把“名称相似”自动视为“变量相同”，也不把“能够间接反映某个过程”视为“可以无条件替代”。判定同时考虑：

1. 物理定义是否一致；
2. WISP 当前模型是否真的使用该通道；
3. 变量是直接输入、代理量，还是需要额外推导；
4. 单位、累计方式和时间尺度是否兼容；
5. 数据源、空间分辨率和预处理是否造成重要语义差异。

## 2. 判定标签

### 2.1 `Yes`

WISP 当前 55 通道输入 schema 中存在物理意义直接对应的变量。例如 SINDBAD 的空气温度 `f_airT` 与 WISP 的 ERA5 2 m air temperature `t2m`。

`Yes` **不代表可以直接复制数值**。数据源、时间分辨率、空间网格、单位、累计定义和归一化仍可能不同，数据交换前仍需转换。

### 2.2 `Approximate`

满足以下至少一种情况：

- WISP 只有相关代理量，而不是同一个物理量；
- WISP 没有独立通道，但可以从现有通道推导；
- 推导需要额外定义，例如 daylight mask、辐射符号约定或时间聚合窗口；
- 推导结果在科学含义上仍不能保证与 SINDBAD forcing 完全一致。

`Approximate` 不能在没有说明和验证的情况下作为 drop-in replacement。

### 2.3 `No`

WISP 当前正式 55 通道 schema 中不存在该物理量，也不存在足够直接、可辩护的替代量。即使 WISP 中有相关变量，只要无法恢复目标量，仍标为 `No`。

## 3. 判定范围与证据

### 3.1 SINDBAD 侧

本文比较两组 WROASTED forcing：

- Spatial setup：`dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_zarr.json`
- Hourly setup：`dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_hourly.json`

Spatial 配置包含 19 个 forcing。Hourly 配置包含 15 个 forcing；原变量表漏掉了 `f_airT_day` 和 `f_VPD_day`，现已补齐。

### 3.2 WISP 侧

WISP 当前训练代码在 `../WildfireIgnitionPred/src/utility.py` 中硬编码 `FEATURE_NAMES_55`。本文只把这个正式 schema 中的通道视为“WISP 中存在”。旧脚本、注释、实验 notebook 或未进入当前训练输入的变量不作为肯定证据。

与本次映射直接相关的 WISP 通道包括：

| WISP channel | WISP 含义 | 主要来源/处理 |
|---|---|---|
| `t2m` | 2 m air temperature | ERA5；训练前由 K 转为 °C |
| `d2m` | 2 m dew-point temperature | ERA5；用于计算 `vpd` |
| `vpd` | vapor pressure deficit | 由 `t2m` 和 `d2m` 计算，单位 kPa |
| `tp` | total precipitation | ERA5 累计量；WISP 通过时间差分恢复时段增量 |
| `ssrd` | surface solar radiation downwards | ERA5 累计辐射；差分并除以 3600 得到近似 W m⁻² |
| `ssr` | surface net solar radiation | ERA5 累计净太阳辐射；不是完整 all-wave net radiation |
| `avg_snswrf` | mean surface net shortwave radiation flux | ERA5 mean flux |
| `avg_snlwrf` | mean surface net longwave radiation flux | ERA5 mean flux |
| `fcover` | fraction of vegetation cover | CLMS FCOVER；裁剪到 [0, 1] |
| `active_fire` | VIIRS active-fire confidence code | `-1` 未观测，`0` 无火，`1/2/3` 为低/中/高置信度火点 |
| `frp` | Fire Radiative Power | VIIRS 活动火点对应的 FRP；不是面积比例 |
| `swvl1`–`swvl4` | volumetric soil water in four soil layers | ERA5 土壤含水量；不是土壤质地 |

WISP 的 ERA5 surface 数据按小时加载，原始 ERA5 网格经过裁剪和重网格到约 375 m VIIRS 网格。CLMS `fcover` 是较低时间频率产品，通过 forward fill 对齐到 WISP 小时时间轴。因此，即便标签为 `Yes`，也不能假定 WISP 与 SINDBAD 的 source product、原生分辨率和时间插值完全一致。

## 4. 完整 crosswalk

| SINDBAD forcing | Spatial source | Hourly source | Status | WISP counterpart |
|---|---|---|---|---|
| `f_ambient_CO2` | `atmCO2_SCRIPPS_global` | `CO2_GF` | `No` | none |
| `f_clay` | `CLYPPT_SoilGrids` | `CLYPPT_SoilGrids` | `No` | none |
| `f_dist_intensity` | `dist_frac_sb2018` | `dist_frac_sb2018` | `No` | none |
| `f_burnt_area` | `fire_frac` | not configured | `Approximate` | `active_fire`, `frp` |
| `f_tree_frac` | `tree_frac` | not configured | `Approximate` | `fcover` |
| `f_frac_vegetation` | `veg_frac` | not configured | `Yes` | `fcover` |
| `f_pft` | `f_pft` | not configured | `No` | none |
| `f_orgm` | `OCSTHA_SoilGrids` | `OCSTHA_SoilGrids` | `No` | none |
| `f_PAR` | `SW_IN_ERAIv2_gfld` | `SW_IN_GF` | `Approximate` | derive from `ssrd` |
| `f_rain` | `P_ERAIv2_gfld` | `P_GF` | `Yes` | `tp` |
| `f_rg` | `SW_IN_ERAIv2_gfld` | `SW_IN_GF` | `Yes` | `ssrd` |
| `f_rg_pot` | `SW_IN_POT_ONEFlux` | `SW_IN_POT_ONEFlux` | `No` | none |
| `f_rn` | `NETRAD_ERAIv2_gfld` | `SW_IN_POT_ONEFlux` | `Approximate` | derive from `avg_snswrf`, `avg_snlwrf` |
| `f_sand` | `SNDPPT_SoilGrids` | `SNDPPT_SoilGrids` | `No` | none |
| `f_silt` | `SLTPPT_SoilGrids` | `SLTPPT_SoilGrids` | `No` | none |
| `f_airT` | `TA_ERAIv2_gfld` | `TA_GF` | `Yes` | `t2m` |
| `f_airT_day` | `TA_DayTime_ERAIv2_gfld` | `TA_GF` | `Approximate` | daytime aggregation of `t2m` |
| `f_VPD` | `VPD_ERAIv2_gfld` | `VPD_GF` | `Yes` | `vpd` |
| `f_VPD_day` | `VPD_DayTime_ERAIv2_gfld` | `VPD_GF` | `Approximate` | daytime aggregation of `vpd` |

## 5. 逐变量详细解释

### 5.1 `f_ambient_CO2` — `No`

**SINDBAD 定义。** Ambient atmospheric CO₂ concentration，范围 200–500 ppm。Spatial setup 使用 `atmCO2_SCRIPPS_global`，hourly setup 使用 `CO2_GF`；两者均表示驱动植被生理过程的环境 CO₂ 浓度。

**WISP 检查。** `FEATURE_NAMES_55` 中没有 CO₂、atmospheric composition、carbon concentration 或可还原 CO₂ 浓度的通道。

**为什么不是代理映射。** `gdmp`、`lai`、`fapar` 等植被状态可能受 CO₂ 影响，但它们同时受气候、养分、植被类型和管理影响，不能反推出 ppm 级环境 CO₂。把植被生产力当作 CO₂ 会混淆驱动与响应变量。

**数据交换要求。** 必须从 Scripps、ERA5/气体产品、站点观测或其他独立 CO₂ 数据源新增 forcing，并转换到 ppm。WISP 当前输入不能提供。

### 5.2 `f_clay` — `No`

**SINDBAD 定义。** Soil clay fraction，来自 SoilGrids `CLYPPT_SoilGrids`，原始单位为百分比，并通过 `source_to_sindbad_unit = 0.01` 转为比例。`spatiovertical` 表示该变量包含土层维度。

**WISP 检查。** WISP 有 `swvl1`–`swvl4` 四层土壤含水量，但没有 clay fraction 或 soil texture class。

**为什么 soil water 不能替代 clay。** Clay 是相对稳定的土壤质地属性；soil water 是随降水、蒸散和排水变化的状态量。Clay 会影响持水曲线，但无法仅从某一时刻或一段时间的 `swvl` 唯一反演。四层结构相似也不意味着物理量相同。

**数据交换要求。** 需要额外加载 SoilGrids clay 数据，统一深度层、百分比/比例单位和目标网格。

### 5.3 `f_dist_intensity` — `No`

**SINDBAD 定义。** 来自 Besnard2018 `dist_frac_sb2018` 的 disturbance fraction/flag，范围约 0–1，并在配置中设为 categorical。它表示生态系统是否或多大程度受到扰动。

**WISP 检查。** WISP 没有 disturbance type、disturbed fraction、harvest、storm damage 或 land-cover transition 通道。

**为什么相关变量不能替代。** `population_density` 只是人类活动压力的粗略 proxy；`active_fire` 只记录火点观测；`fcover`/`lai` 的下降可能由火、干旱、采伐、季节变化或数据缺失造成。它们不能恢复 Besnard2018 disturbance flag 的定义。

**数据交换要求。** 必须引入与 SINDBAD 定义一致的 disturbance dataset，或重新定义一个经过验证的 WISP-specific disturbance forcing；后者不能声称与原变量等价。

### 5.4 `f_burnt_area` — `Approximate`

**SINDBAD 定义。** `fire_frac` 表示一个像元内 burned/fire area fraction，范围 0–1，是面积比例。

**WISP 候选。** `active_fire` 记录 VIIRS 活动火点置信度；`frp` 记录对应活动火点的 Fire Radiative Power。WISP 还会把未来 24 小时强火点生成 union target，但该 target 仍是“期间是否检测到火”的二值结果，不是烧毁面积比例产品。

**为什么只能 `Approximate`。** Active-fire detection 是瞬时/过境时段的热异常观测；burned area 是火后地表变化形成的面积量。未被卫星过境捕获的燃烧、云遮挡、火点大小低于像元、同一位置重复观测等都会使二者不同。FRP 是功率，不是面积；置信度代码也不是面积分数。

**可能的近似方法。** 可以把一个较粗目标像元内、指定时间窗口中出现强火点的 WISP 子像元面积占比作为 proxy，但必须明确：

- 选择哪些置信度等级；
- 如何处理 `-1` 未观测；
- 时间窗口和重复观测如何聚合；
- VIIRS 375 m detection footprint 与目标像元面积如何对应；
- 结果未经 burned-area product 校准时不能称为真实 burned fraction。

**更可靠方案。** 引入 MCD64A1、FireCCI 或其他正式 burned-area 产品，与 SINDBAD 的时间窗口及网格定义对齐。

### 5.5 `f_tree_frac` — `Approximate`

**SINDBAD 定义。** `tree_frac` 是树木覆盖比例，范围 0–1。它要求将 tree cover 与草本、灌丛、作物等其他植被区分开。

**WISP 候选。** `fcover` 来自 CLMS FCOVER，表示总的 green vegetation cover fraction。WISP 还有 `lai`、`fapar` 和 `gdmp`，但这些分别描述叶面积、吸收光合有效辐射比例和生产力，而不是树木比例。

**为什么只能 `Approximate`。** 一个草地和一个稀疏林地可以具有相近 `fcover`，但 tree fraction 完全不同。WISP 当前没有 land-cover/PFT 通道用于把 `fcover` 分解为 tree、grass、shrub 或 crop。

**数据交换要求。** 若仅需 vegetation cover proxy，可使用 `fcover` 并保留 `Approximate` 标识；若 SINDBAD 模块确实依赖 woody/tree fraction，则必须增加 tree-cover 或 land-cover 数据。

### 5.6 `f_frac_vegetation` — `Yes`

**SINDBAD 定义。** Source variable 为 `veg_frac`，范围 0–1，意图表示 vegetation-covered fraction。配置中的 `standard_name` 写成 `tree fraction`，与变量名和 source variable 不一致；本判定按 `f_frac_vegetation`/`veg_frac` 的变量语义处理。

**WISP 对应。** `fcover` 来自 CLMS FCOVER，同样表示 vegetation cover fraction，并在 WISP 预处理中裁剪到 [0, 1]。

**为什么是 `Yes`。** 两者的核心物理量都是像元被植被覆盖的比例，WISP 存在直接对应通道，不需要从其他变量间接推断。

**仍需处理的差异。** `Yes` 不保证两个产品的植被定义、观测日期、质量控制、原生分辨率或缺测填补相同。WISP 将低频 CLMS 产品 forward-fill 到小时时间轴；SINDBAD spatial 配置将该变量标为 `spatiovertical`。交换前必须核实维度、产品定义、时间对齐和重网格方法。

### 5.7 `f_pft` — `No`

**SINDBAD 定义。** Plant Functional Type index，类别 1–17，是离散 categorical land/vegetation type。

**WISP 检查。** WISP 有 `lai`、`fapar`、`fcover`、`gdmp` 等连续 vegetation-state 通道，但没有 PFT index 或 land-cover class 通道。

**为什么 vegetation state 不能恢复 PFT。** 多种 PFT 可以有相近 LAI、FAPAR 或 FCOVER，同一 PFT 也会随季节和胁迫表现出很大变化。连续状态变量到离散 PFT 不是一一映射。

**数据交换要求。** 需要加载与 SINDBAD 17 类定义一致的 PFT 产品；若使用其他 land-cover taxonomy，还需要显式 crosswalk 和不可映射类别处理。

### 5.8 `f_orgm` — `No`

**SINDBAD 定义。** Source variable `OCSTHA_SoilGrids` 指向 SoilGrids organic carbon/organic matter 类土壤属性，范围配置为 0–100，并带土层维度。

**配置注意。** `standard_name` 被写为 `CLAY`，明显与 `f_orgm` 和 `OCSTHA` 不一致；`source_to_sindbad_unit` 还被设置为 `0.0`。这意味着当前配置在单位换算上可能把输入全部乘为零，必须由 SINDBAD 配置维护者确认。

**WISP 检查。** WISP 没有 soil organic carbon、organic matter、carbon stock 或可直接转换的通道。`gdmp` 是 vegetation productivity，不是土壤碳；`swvl` 是土壤水分。

**数据交换要求。** 必须额外读取 SoilGrids organic carbon/stock 数据，并先澄清 SINDBAD 期望的是 concentration、content 还是 stock，以及正确的单位换算系数。

### 5.9 `f_PAR` — `Approximate`

**SINDBAD 定义。** Photosynthetically Active Radiation。Spatial 配置明确采用 `PAR = Rg × 0.5`，以 `SW_IN_ERAIv2_gfld` 为源，单位 MJ m⁻² d⁻¹。Hourly 配置以 `SW_IN_GF` 为源，并使用 0.0018 换算，等价于短波辐射乘 0.5 后再进行 W m⁻² 到 MJ m⁻² h⁻¹ 的小时换算。

**WISP 候选。** `ssrd` 是 ERA5 surface solar radiation downwards。WISP 没有独立 PAR 通道。

**为什么是 `Approximate`。** 可以遵循 SINDBAD 自身假设用 `0.5 × ssrd` 估算 PAR，但 0.5 是宽波段短波到 PAR 的经验比例，不是直接光谱观测。比例会受太阳高度、大气和云条件影响。

**WISP 预处理影响。** ERA5 `ssrd` 通常是累计能量 J m⁻²。WISP 预处理先做时间差分，再除以 3600，转换为近似 W m⁻²。因此：

- 若从原始 WISP/ERA5 cube 生成小时 PAR，应对累计量正确差分，再乘 0.5，并转换为目标能量单位；
- 若从已 transform 的 WISP 模型输入读取，必须确认它是否已被缩放、`zscore` 或其他变换，不能把归一化数值直接当物理辐射；
- 日尺度 PAR 需要对小时能量求和，而不是简单平均 W m⁻² 数值。

### 5.10 `f_rain` — `Yes`

**SINDBAD 定义。** Spatial 配置使用日降水 `P_ERAIv2_gfld`，单位 mm d⁻¹；hourly 配置使用 `P_GF`，单位 mm h⁻¹。

**WISP 对应。** `tp` 是 ERA5 total precipitation，是 WISP 正式 surface weather 通道。

**为什么是 `Yes`。** WISP 存在直接降水量通道，可作为 SINDBAD precipitation/rain forcing 的物理对应变量。

**重要限制。** ERA5 `tp` 是 total precipitation，可能包含液态和固态降水；SINDBAD 字段名写作 rain。如果应用区域存在降雪并且 SINDBAD 严格要求 liquid rain，需要额外进行相态分离，不能无条件把全部 `tp` 当雨。

**单位与累计处理。** WISP 对累计 `tp` 做 `diff_time` 获取时段增量，并可能进一步 `log1p`/标准化。交换到 SINDBAD 时应从物理量阶段取值：

- 小时值：恢复每小时增量并转换为 mm h⁻¹；
- 日值：按日累加小时增量，转换为 mm d⁻¹；
- 对累计重置或负差分必须按 ERA5 accumulation convention 处理；
- 不得直接使用 `log1p` 或 z-score 后的模型输入值。

### 5.11 `f_rg` — `Yes`

**SINDBAD 定义。** Global radiation，source variable 为 incoming shortwave radiation：spatial 使用 `SW_IN_ERAIv2_gfld`，hourly 使用 `SW_IN_GF`。

**WISP 对应。** `ssrd` 是 surface solar radiation downwards，即到达地表的向下太阳短波辐射。

**为什么是 `Yes`。** 两者核心物理定义直接对应，都是 surface incoming/downward shortwave radiation。

**单位与时间处理。** SINDBAD 需要 MJ m⁻² per day 或相应小时能量；WISP 处理中 `ssrd` 从累计 J m⁻² 差分并除以 3600 转为近似 W m⁻²。必须根据目标时间步积分或求和，不能把瞬时/平均通量直接标成每日能量。

### 5.12 `f_rg_pot` — `No`

**SINDBAD 定义。** Potential Global Radiation，source variable `SW_IN_POT_ONEFlux`，表示在指定潜在/参考条件下的可用全球辐射，而不是实际受当时云和大气影响的观测辐射。

**WISP 检查。** WISP 有实际 ERA5 `ssrd`，但没有 `SW_IN_POT`、clear-sky radiation 或 potential radiation 通道。

**为什么 `ssrd` 不能直接替代。** 实际 incoming radiation 包含云、气溶胶和天气状态的影响；potential radiation 通常由太阳几何和参考大气条件定义。两者在阴天可显著不同。使用实际 `ssrd` 会改变 `Rg/Rg_pot` 等辐射胁迫或云量相关指标的含义。

**数据交换要求。** 需要引入 ONEFlux potential radiation，或按照经纬度、日期、太阳几何和明确的 clear-sky 假设重新计算。WISP 现有 55 通道本身不足以提供该量。

### 5.13 `f_rn` — `Approximate`

**SINDBAD 定义。** Net radiation，spatial source 为 `NETRAD_ERAIv2_gfld`，单位 MJ m⁻² d⁻¹。完整地表净辐射通常包括净短波和净长波分量。

**WISP 候选。** WISP 有：

- `ssr`：surface net solar radiation，仅净短波；
- `avg_snswrf`：mean surface net shortwave radiation flux；
- `avg_snlwrf`：mean surface net longwave radiation flux。

**为什么是 `Approximate`。** `ssr` 单独缺少长波分量，`ssrd` 更只是向下短波，因此二者都不能单独代表完整 net radiation。原则上可以计算：

```text
Rn ≈ avg_snswrf + avg_snlwrf
```

但必须先验证两个源产品的正负方向、平均周期、时间戳语义和单位。WISP 代码注释将这两个变量描述为 mean flux，其中所用文件还可能是 monthly mean；这与逐小时或逐日同步 net radiation 不一定等价。

**Hourly 配置异常。** `forcing_hourly.json` 把 `f_rn.standard_name` 写为 `Net radiation`，但 source variable 是 `SW_IN_POT_ONEFlux`，即 potential incoming shortwave。两者物理定义不一致。本文保留配置原值，不擅自将其中之一视为正确；使用前必须确认这是复制错误还是有意设置。

**数据交换要求。** 核对 WISP 两个净辐射分量的 metadata 后求和，再将 W m⁻² 按目标时间步积分到 MJ m⁻²。若需要严格逐小时 `Rn`，优先读取同时间分辨率的 ERA5 shortwave/longwave components，而不是 monthly mean proxy。

### 5.14 `f_sand` — `No`

**SINDBAD 定义。** Soil sand fraction，来自 `SNDPPT_SoilGrids`，百分比乘 0.01 转为比例，带土层维度。

**WISP 检查。** 没有 sand fraction。`swvl1`–`swvl4` 是含水量；地形变量 `elevation`、`slope`、`hand`、`geomorphon_*` 也不能识别颗粒组成。

**数据交换要求。** 新增 SoilGrids sand fraction，并对齐土层、网格和单位。

### 5.15 `f_silt` — `No`

**SINDBAD 定义。** Source variable `SLTPPT_SoilGrids` 表示 soil silt fraction，百分比乘 0.01 转为比例，带土层维度。

**配置注意。** `standard_name` 写为 `CLAY`，与 `f_silt` 和 `SLTPPT` 不一致，应视为元数据错误候选，但本次不修改配置。

**WISP 检查与限制。** WISP 没有 silt fraction。土壤水分和地形不能唯一恢复 silt content。

**数据交换要求。** 新增 SoilGrids silt fraction，并核实 clay+sand+silt 的单位和总和规则。

### 5.16 `f_airT` — `Yes`

**SINDBAD 定义。** Near-surface air temperature。Spatial 使用 `TA_ERAIv2_gfld`，hourly 使用 `TA_GF`，SINDBAD 单位 °C。

**WISP 对应。** `t2m` 是 ERA5 2 m air temperature，是 WISP 正式 weather channel。

**为什么是 `Yes`。** 两者都是近地面 2 m/站点高度附近的空气温度，物理定义直接对应。

**转换要求。** WISP 原始 ERA5 `t2m` 通常为 K，预处理转为 °C。必须在归一化前的物理量阶段导出；如果使用标准化后的模型 tensor，需要反变换。还应明确空间插值和站点/网格高度差异。

### 5.17 `f_airT_day` — `Approximate`

**SINDBAD 定义。** Daytime air temperature。Spatial setup 直接使用 `TA_DayTime_ERAIv2_gfld`；hourly setup 仍从 `TA_GF` 读取，但把它作为独立 forcing 键 `f_airT_day`。

**WISP 候选。** WISP 只有逐小时 `t2m`，没有 `airT_day` 独立通道。

**为什么是 `Approximate`。** 可以从小时 `t2m` 计算白昼统计，但结果取决于尚未在 WISP schema 中定义的规则：

- 白昼是固定当地小时窗口，还是太阳高度大于 0；
- 使用 UTC、当地标准时间还是真太阳时；
- 计算 daytime mean、maximum、integral 还是其他统计；
- 极昼、极夜、缺测小时如何处理。

在这些规则与 SINDBAD `TA_DayTime` 产品定义完全对齐前，只能标记为可推导近似。

**数据交换要求。** 先读取 `t2m` 的物理 °C 值，基于经纬度和时间建立明确 daylight mask，按 SINDBAD 相同统计定义聚合，并记录时区和缺测规则。

### 5.18 `f_VPD` — `Yes`

**SINDBAD 定义。** Vapor Pressure Deficit，单位 kPa。Spatial source 为 `VPD_ERAIv2_gfld`，hourly source 为 `VPD_GF`。

**WISP 对应。** `vpd` 是 WISP 正式 weather channel。WISP 不直接从文件读取它，而是由 `t2m` 和 `d2m` 计算：

```text
T  = t2m - 273.15
Td = d2m - 273.15
es = 0.6108 × exp(17.27 × T  / (T  + 237.3))
ea = 0.6108 × exp(17.27 × Td / (Td + 237.3))
vpd = max(es - ea, 0)
```

结果单位为 kPa。

**为什么是 `Yes`。** 虽然 WISP 的 VPD 是 derived channel，但它已经作为独立 `vpd` 通道进入正式 55 通道 schema，物理定义与 SINDBAD VPD 对应。

**转换要求。** WISP 后续可能对 `vpd` 做 `log1p` 和标准化，因此应从变换前或可逆变换后的物理值导出。还需注意 SINDBAD lower bound 为 0.01 kPa，而 WISP 计算允许 0；对齐时要明确是否 clip 到 0.01。

### 5.19 `f_VPD_day` — `Approximate`

**SINDBAD 定义。** Daytime Vapor Pressure Deficit。Spatial setup 使用 `VPD_DayTime_ERAIv2_gfld`；hourly setup 从 `VPD_GF` 提供该 forcing。

**WISP 候选。** WISP 有逐小时 `vpd`，没有独立 `vpd_day` 通道。

**为什么是 `Approximate`。** 与 `f_airT_day` 相同，必须先定义 daylight mask 和聚合统计。简单日平均会包含夜间，不能自动等价于 daytime VPD。若从日平均温度和露点重新计算 VPD，还会因饱和水汽压的非线性而与逐小时 VPD 的白昼平均不同。

**数据交换要求。** 应先对每个小时从 `t2m`/`d2m` 计算物理 kPa VPD，再应用与 SINDBAD 一致的白昼掩膜和统计，而不是先平均温度后计算 VPD。最后按需要执行 0.01 kPa lower-bound clipping。

## 6. Spatial 与 hourly 配置的关键差异

### 6.1 变量集合

Spatial setup 有 19 个 forcing，并额外包括 fire/vegetation/PFT 变量：`f_burnt_area`、`f_tree_frac`、`f_frac_vegetation` 和 `f_pft`。Hourly setup 没有这四项，因此为 15 项。

### 6.2 Source variable

Spatial setup 主要使用 ERAIv2/global 产品名，例如 `TA_ERAIv2_gfld`、`P_ERAIv2_gfld` 和 `SW_IN_ERAIv2_gfld`。Hourly setup 主要使用站点/FLUXNET 风格的 `*_GF` 变量，例如 `TA_GF`、`P_GF`、`SW_IN_GF` 和 `VPD_GF`。

WISP 的候选 weather 通道来自自身 ERA5 preprocessing pipeline。相同物理量不意味着三个来源的质量控制、gap filling 或网格代表性相同。

### 6.3 时间尺度和单位

- Spatial `f_rain`：mm d⁻¹；hourly `f_rain`：mm h⁻¹。
- Spatial radiation：MJ m⁻² d⁻¹；hourly source 常为 W m⁻²，并通过 conversion factor 转为时段能量。
- WISP `tp`、`ssr`、`ssrd` 在原始 ERA5 文件中可能采用累计量表达，预处理通过 `diff_time` 得到时段值。
- 日量应由小时增量累计；平均通量与累计能量必须通过时间长度显式换算。

### 6.4 空间表达

SINDBAD spatial setup 的配置空间维度是 site，并包含一些 `spatiovertical` soil/vegetation 属性。WISP 使用 region cube，ERA5/CLMS/VIIRS 数据被对齐到约 375 m 网格。两者之间需要 site extraction 或 grid aggregation，不能只改变量名。

## 7. 配置元数据异常与待确认事项

以下问题来自现有 SINDBAD 配置，本文只记录，不修改原配置：

1. `f_frac_vegetation.standard_name` 是 `tree fraction`，但变量键和 source variable 分别是 `f_frac_vegetation`、`veg_frac`。
2. `f_orgm.standard_name` 是 `CLAY`，但 source variable 是 `OCSTHA_SoilGrids`。
3. `f_silt.standard_name` 是 `CLAY`，但 source variable 是 `SLTPPT_SoilGrids`。
4. Hourly `f_rn.standard_name` 是 `Net radiation`，source variable 却是 `SW_IN_POT_ONEFlux`。
5. `f_orgm.source_to_sindbad_unit` 是 `0.0`；若该系数实际参与乘法换算，forcing 将全部变为零。
6. Hourly `f_PAR.standard_name` 文本写有 `Rg*.5 * 0.0036`，但配置 conversion factor 为 `0.0018`；数学上二者一致，但应明确它表示一个小时的 W m⁻² 到 MJ m⁻² h⁻¹ 转换，而不是日累计。

这些问题会影响科学解释和数值结果，正式耦合前应逐项由配置维护者确认。

## 8. 能否直接用于 SINDBAD

### 8.1 有直接物理对应，但仍需转换

| SINDBAD | WISP | 必需处理 |
|---|---|---|
| `f_frac_vegetation` | `fcover` | 产品定义核对、时间对齐、网格/site 聚合 |
| `f_rain` | `tp` | 累计差分、相态确认、小时/日单位转换 |
| `f_rg` | `ssrd` | 累计差分、通量到能量转换、小时/日聚合 |
| `f_airT` | `t2m` | K→°C（若读取原始值）、网格/site 对齐 |
| `f_VPD` | `vpd` | 使用物理 kPa 值、撤销模型变换、lower-bound 处理 |

### 8.2 可以推导或作为 proxy，但必须验证

| SINDBAD | WISP basis | 核心风险 |
|---|---|---|
| `f_burnt_area` | `active_fire`, `frp` | 活动火不是烧毁面积；存在过境和未观测偏差 |
| `f_tree_frac` | `fcover` | 无法区分树与其他植被 |
| `f_PAR` | `0.5 × ssrd` | 经验光谱比例、累计量和单位处理 |
| `f_rn` | `avg_snswrf + avg_snlwrf` | 符号、时间平均周期、配置 source 异常 |
| `f_airT_day` | hourly `t2m` | daylight definition 和聚合统计未定义 |
| `f_VPD_day` | hourly `vpd` | daylight definition、非线性和聚合顺序 |

### 8.3 必须增加外部数据

`f_ambient_CO2`、`f_clay`、`f_dist_intensity`、`f_pft`、`f_orgm`、`f_rg_pot`、`f_sand` 和 `f_silt` 无法从 WISP 当前输入可靠恢复。若 SINDBAD 模型结构实际调用这些 forcing，就必须增加相应外部数据，而不是用弱相关 WISP 通道填充。

## 9. 推荐的数据交换顺序

如果后续要把 WISP 数据真正转换成 SINDBAD forcing，推荐按以下顺序实施：

1. 从 WISP **物理量阶段**而不是归一化模型 tensor 读取 `t2m`、`tp`、`ssrd`、`vpd`、`fcover` 和辐射分量。
2. 明确目标是 spatial/day forcing 还是 hourly forcing，按目标时间步处理累计量和通量。
3. 为 site 提取或对目标像元聚合，并记录使用 mean、sum、area-weighted mean 还是 nearest-neighbor。
4. 对 `f_PAR`、`f_rn` 和 daytime variables 编写独立、可测试的推导函数，禁止隐式单位换算。
5. 对 `Approximate` 变量在输出 metadata 中保留 proxy/derived 标记和方法说明。
6. 对 `No` 变量从外部产品补齐；在缺失前不要用零值冒充真实 forcing。
7. 最后执行 bounds、单位、时间轴、缺测值和维度检查，再交给 SINDBAD。

## 10. 证据文件

### SINDBAD

- [Spatial forcing configuration](dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_zarr.json)
- [Hourly forcing configuration](dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_hourly.json)

### WISP

- [Current 55-channel schema](../WildfireIgnitionPred/src/utility.py)
- [ERA5/CLMS/VIIRS preprocessing implementation](../WildfireIgnitionPred/preprocessing_code/preprocess_utils.py)
- [Physical transforms and normalization](../WildfireIgnitionPred/preprocessing_code/normalize_data.py)
- [Current preprocessing protocol](../WildfireIgnitionPred/Data_Preprocessing_Protocol.md)
- [Current pipeline reference](../WildfireIgnitionPred/Method_Reference_CurrentPipeline.md)

## 11. 最终结论

WISP 可以直接提供 SINDBAD 所需 forcing 中的空气温度、VPD、总降水、向下短波辐射和植被覆盖率对应量，但仍必须完成单位、时间和空间对齐。PAR、完整净辐射、白昼温度、白昼 VPD、树木比例和 burned-area fraction 只能近似推导或使用 proxy。CO₂、土壤质地、土壤有机质、PFT、扰动强度和 potential radiation 在 WISP 当前正式输入中缺失，必须从外部数据源补充。
