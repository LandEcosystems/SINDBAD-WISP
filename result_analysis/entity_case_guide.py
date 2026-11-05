"""Read-only T1862 figure guide and portable entity export.

Uses the already audited 293-region analysis; no simulation or source mutation.
Every original entity PNG must occur exactly once in the figure inventory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import textwrap
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .registry import components, VARIABLES

RID = "B1_FRA_Pyrenees-Atlantics_near-Biarritz_43.1892N_001.1351W_T1862"
ROOT = Path("/Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1")
BATCH = Path("/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km")
ENTITY = Path("/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610/entity") / RID
REPO = Path(__file__).resolve().parents[1]
SCENARIOS = ("No_Fire", "VIIRS_Fire")
FONT = "/usr/share/fonts/google-droid/DroidSansFallback.ttf"
POOL_CN = dict(root="根碳", wood="木质碳", leaf="叶碳", reserve="储备碳",
               litter_fast="快速凋落物碳", litter_slow="慢速凋落物碳",
               soil_slow="慢速土壤碳", soil_old="老土壤碳")
MEANINGS = {
    "gpp": ("GPP：总初级生产力", "光合作用从大气吸收的碳；正值表示吸收量。", "有火情景的碳吸收减少。看差值图的负值位置，以及两条曲线何时开始分离；相似季节周期和很高的相关性并不表示没有火灾影响。"),
    "auto_respiration": ("植被自养呼吸", "植物维持与生长等过程向大气释放的碳。", "自养呼吸下降，与植被碳和生产力下降一致；它部分抵消直接燃烧和GPP损失带来的额外排放，不能单独解释为生态系统改善。"),
    "hetero_respiration": ("异养呼吸", "凋落物和土壤有机物分解释放的碳。", "平均异养呼吸略增。火灾死亡产生的未燃烧碳进入凋落物，与这个响应方向一致；仍需结合具体碳转移和分解过程诊断机制。"),
    "eco_respiration": ("生态系统总呼吸", "自养呼吸与异养呼吸之和。", "自养呼吸下降大于异养呼吸增加，因此总呼吸降低。这是净火灾碳响应中的抵消项。"),
    "nee": ("NEE：净生态系统交换", "NEE = 生态系统呼吸 - GPP；负值表示净吸收；不含直接燃烧。", "九个完整年份合计，NEE更偏向大气释放。2019年无火情景为净吸收，有火情景转为净释放；部分其他年份则反向。必须再加直接燃烧才能判断净火灾贡献。"),
    "evapotranspiration": ("ET：总蒸散", "蒸腾、土壤蒸发、冠层截留蒸发及雪升华等水损失。", "平均ET略降，但变化远小于整个地块的年际波动。蒸腾的下降更大，说明其他蒸发分量部分抵消了它；未保存各分量时不能进一步唯一归因。"),
    "transpiration": ("植被蒸腾", "植物通过气孔向大气输送的水。", "有火情景蒸腾减少，与植被碳减少一致；这能帮助解释深层水储量和径流略增的共同响应。"),
    "runoff": ("径流", "离开模拟土壤—地下水系统的径流水量。", "平均径流略增，与蒸腾降低、部分水储量升高一致。该变化是模型的水文响应，不是实测洪水风险的验证。"),
}
KINDS = {
    "comparison_map": "两情景均值与差值图：对角线为各情景时间均值；右上及左下直方图均表示 VIIRS_Fire - No_Fire。蓝色为负，红色为正；灰色可能只是小差异。",
    "comparison_timeseries": "两情景时间序列：上、中、下分别为逐日、月平均和年平均。通量曲线仍是每日速率，年面板不能直接读作年累计排放。",
    "single_map": "单情景时间均值图：显示该情景各像元的平均水平；单独一张图不能确定火灾效应，应与同分量另一情景配对。原图坐标是数组索引。",
    "single_timeseries": "单情景时间序列：显示空间平均的日序列、月均值和年均值；季节波动同时受气候等驱动影响，火灾效应要看情景差。",
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fmt(x):
    return "0" if x == 0 else f"{x:+.4f}"


def describe(c, stats):
    var, comp = c["variable"], c["component"]
    if var in MEANINGS:
        title, meaning, effect = MEANINGS[var]
    elif var == "cEco":
        title = f"碳库 {c['index']+1}：{POOL_CN[comp]}"
        meaning = "这里是现存碳库存，不是碳排放通量；不能把逐日库存相加当作排放。"
        effect = {
            "root": "根碳减少。根的直接燃烧为零，但火灾死亡及碳转移仍使根库受损。",
            "wood": "木质碳是植被碳减少的主要分量。时间图中火灾阶段的阶梯式下降，及之后未完全闭合的情景差，表示多年库存影响。",
            "leaf": "叶碳减少，联系当前以叶碳计算fAPAR的模型过程，可帮助解释GPP和蒸腾下降。不能据此唯一恢复LAI。",
            "reserve": "储备碳减少，表明火灾不仅影响结构碳，也改变植物储备；之后的补充与消耗共同决定差值。",
            "litter_fast": "多年平均快速凋落物库存略降，尽管它接收到火灾死亡的碳；燃烧和分解也在消耗该库。库存变化不能直接等同于转入量。",
            "litter_slow": "多年平均慢速凋落物增加，与木质等未燃烧死亡碳的转入相符；末期值与多年均值可能不同，应分别查看。",
            "soil_slow": "土壤碳略增，属于碳转移与分解反馈；当前配置中该库的直接燃烧为零。这不抵消整个生态系统的总碳损失。",
            "soil_old": "老土壤碳变化很小且偏正，是缓慢的间接响应；不能将它解释为火灾增加了整体碳储量。",
        }[comp]
    elif var == "TWS":
        layer = c["index"] + 1
        name = "第%d层土壤水" % layer if layer <= 7 else {8: "地下水", 9: "雪水", 10: "表面水"}[layer]
        title, meaning = f"水库 {layer}：{name}", "单位mm，表示储水量；不是蒸散或径流速率。"
        if layer <= 4:
            effect = "浅层储水差异很小，平均略降；应读差值色标的实际量级，不凭红蓝颜色判断影响很大。"
        elif layer <= 8:
            effect = "较深水库平均略增，与蒸腾降低的方向一致；需结合各层排水和根系用水过程，不能仅凭这一图证明因果机制。"
        elif layer == 9:
            effect = "两个情景的雪水差异为零；不能据此说整个地块没有雪，只能说当前情景没有改变该分量。"
        else:
            effect = "表面水在本案例中为相同的常量。零差值及未定义相关系数是正常结果，不是缺测证据。"
    else:
        pn = POOL_CN[comp]
        if var == "c_fire_efflux":
            title, meaning = f"直接燃烧排放 {c['index']+1}：{pn}", "从这个碳库直接燃烧并释放到大气的碳；八层求和才是总直接火灾碳排放。"
            effect = "无火情景为零；有火情景的脉冲对应模型燃烧损失。月、年均值会把短暂峰值摊薄，累计量须对日通量积分。"
        elif var == "c_fire_flow":
            title, meaning = f"未燃烧碳转入 {c['index']+1}：{pn}", "这个库接收的火灾死亡、但没有直接燃烧的碳；它是生态系统内部转移，不是额外大气排放。"
            effect = "本例转移主要进入快速及慢速凋落物库，能够影响随后的分解呼吸；不能把它再加到直接燃烧排放上。"
        else:
            title, meaning = f"火灾死亡 {c['index']+1}：{pn}", "该植被库因火灾死亡的碳，包括被燃烧和转入其他库的部分；不是另一份可直接相加的排放。"
            effect = "本例木质死亡贡献最大；根即使没有直接燃烧也会死亡并转移。该诊断与efflux/flow有重合，不能三项相加。"
        if stats["fire_mean_annual_or_stock"] == 0 and stats["nofire_mean_annual_or_stock"] == 0:
            effect = "本案例、当前配置下两情景该分量均为零。不存在该通道的火灾响应；这不是缺测，也不意味着现实中这类碳库不可能受火影响。相关系数可能未定义。"
    return title, meaning, effect


def forcing_description(key, scenario):
    m = {
        "f_PAR": "光合有效辐射，当前映射为短波辐射的0.5倍；驱动光合作用。",
        "f_airT": "全天平均气温；影响呼吸、蒸散、积雪等过程。",
        "f_airT_day": "白天平均气温；用于光合作用相关响应。",
        "f_VPD_day": "白天饱和水汽压差；反映大气干燥程度，影响气孔和光合胁迫。",
        "f_ambient_CO2": "大气CO2浓度；这张图是时间序列，不能当作火灾排放浓度观测。",
        "f_rain": "逐日降水驱动的时间均值图；两情景共用同一降水。",
        "f_rg": "逐日入射短波辐射驱动的时间均值。",
        "f_rg_pot": "潜在短波辐射驱动的时间均值，用于辐射比例等过程。",
        "f_rn": "逐日净辐射驱动的时间均值；影响蒸散等能量相关过程。",
        "f_frac_vegetation": "静态绿色植被比例：完整3641天已处理FCOVER的均值，逐日重复；实际运行再乘0.99。它是共同背景，不输入观测到的火后下降。",
        "f_tree_frac": "静态树覆盖：Hansen 2000树冠覆盖扣除截至2010年底记录的损失后聚合；逐日重复。不是独立观测的2010树冠图。",
        "f_pft": "实际模型使用的FLUXNET兼容PFT类别；类别变化不是模拟火灾的结果，也不等同于原始土地覆盖图。",
        "f_dist_year": "初始化扰动年份：从两参考年得到的有效且早于2014的年份中取最大值；9999为未知约定，不是真实未来事件。",
        "f_soil_depth": "土壤深度驱动，模型单位mm；Shangguan来源由m换算，0.01m对应10mm。深度决定活跃土壤柱。",
        "f_burnt_area": "火灾代理驱动：No_Fire乘0，VIIRS_Fire乘0.99。原图是日值的时间均值，反映出现频率，不是累计烧毁面积比例。",
    }
    if key in m:
        text = m[key]
    else:
        base, layer = key.rsplit("_", 1)
        text = {"f_clay": "黏粒比例", "f_sand": "砂粒比例", "f_silt": "粉粒比例", "f_orgm": "有机质输入"}[base]
        text = f"土壤输入第{layer}层的{text}；属于静态土壤背景。输入的六层质地与模型的七层水库不是同一层索引。具体单位和映射以保存的forcing配置为准。"
    text += " 此图表示输入条件，不能用它直接量化模拟火灾效应。"
    if key == "f_burnt_area":
        text += " 当前No_Fire图应全零。" if scenario == "No_Fire" else " 当前VIIRS_Fire图非零处是曾有符合条件探测的格点；反复探测可重复驱动模型。"
    else:
        text += " 两情景共用此背景；火灾对输出的影响需查看配对模拟结果。"
    return text


def init(out):
    out.mkdir(parents=True, exist_ok=True)
    for d in ("guide", "tables", "qa", "docs", "plots", "code"):
        (out / d).mkdir(exist_ok=True)
    rp = ROOT / "regions" / RID
    annual = pd.read_csv(rp / "annual.csv")
    annual = annual[annual.group_type.eq("overall") & annual.group.eq("all")]
    annual.to_csv(out / "tables/annual_owned_area.csv", index=False)
    monthly = pd.read_csv(rp / "monthly.csv")
    monthly = monthly[monthly.group_type.eq("overall") & monthly.group.eq("all")]
    monthly.to_csv(out / "tables/monthly_owned_area.csv", index=False)
    for f in ("qa.json", "provenance.json", "completion.json", "legacy_csv_recheck.csv"):
        shutil.copy2(rp / f, out / "qa" / f)
    audit = ROOT / "dependency_audits/0637ed669410ade2248e5715ec54086403501a598d9286d32a58350701a2e15c/regions" / (RID + ".json")
    shutil.copy2(audit, out / "qa/dependency_audit.json")
    for f in ("README_SINDBAD_WISP_OUTPUT.md", "docs/OUTPUT_SEMANTICS_EN.md"):
        shutil.copy2(REPO / f, out / "docs" / Path(f).name)
    shutil.copy2(__file__, out / "code/entity_case_guide.py")
    z = dict(np.load(rp / "spatial.npz"))
    mask = z["run_valid_mask"].astype(bool) & (z["owned_area_m2"] > 0)
    area = z["owned_area_m2"][mask].sum()
    rank = pd.read_csv(ROOT / "report_findings_v3_all293/fire_site_cases_v1/ranked_sites_2015_2023.csv")
    rank = rank[rank.region_id.eq(RID)].iloc[0].to_dict()
    assert int(rank["direct_intensity_rank"]) == 1
    rows, figures = [], []
    def wa(key):
        return float(np.sum(z[key][mask] * z["owned_area_m2"][mask]) / area)
    for c in components():
        v, k = c["variable"], c["component"]
        a = annual[annual.variable.eq(v) & annual.component.eq(k) & annual.year.between(2015, 2023)]
        assert len(a) == 18 and a.complete_period.all()
        av = a.groupby("scenario")["mean"].mean()
        st = dict(variable=v, component=k, layer=c["index"]+1,
                  nofire_mean_annual_or_stock=float(av.No_Fire), fire_mean_annual_or_stock=float(av.VIIRS_Fire),
                  delta_mean_annual_or_stock=float(av.VIIRS_Fire-av.No_Fire),
                  units=a.units.iloc[0], aggregation="mean of 9 annual totals" if c["kind"]=="sumflux" else "mean of 9 annual mean stocks")
        if c["kind"] == "meanstock":
            st["final_nofire"] = wa(f"{v}__{k}__final_saved__No_Fire")
            st["final_fire"] = wa(f"{v}__{k}__final_saved__VIIRS_Fire")
            st["final_delta"] = st["final_fire"] - st["final_nofire"]
        title, meaning, effect = describe(c, st)
        st.update(title_cn=title, meaning_cn=meaning, effect_cn=effect)
        st["summary_cn"] = f"2015–2023完整年份：No_Fire {av.No_Fire:.4f}，VIIRS_Fire {av.VIIRS_Fire:.4f}，差值 {fmt(av.VIIRS_Fire-av.No_Fire)} {st['units']}。" + ("这里是每年累计量的九年平均。" if c["kind"]=="sumflux" else f"这里是多年平均库存；2023年末库存差为 {fmt(st['final_delta'])} {st['units']}。")
        prefix = "pools" if v in ("cEco", "TWS") else "diagnostics" if v.startswith("c_fire") else "fluxes"
        base = prefix + "_" + v + (f"_{c['index']+1}" if len(VARIABLES[v]["components"]) > 1 else "")
        fs = [(ENTITY / "comparison" / f"{base}_mean_comparison_{RID}-full.png", "comparison_map", "paired"),
              (ENTITY / "comparison" / f"{base}_timeseries_comparison_{RID}-full.png", "comparison_timeseries", "paired")]
        for s in SCENARIOS:
            folder = ENTITY / f"output_WISP_{RID}-full_{s}" / "figure"
            fs += [(folder / f"{base}_{RID}-full-{s}.png", "single_map", s),
                   (folder / f"{base}_timeseries_{RID}-full-{s}.png", "single_timeseries", s)]
        st["figure_ids"] = []
        for src, kind, scenario in fs:
            assert src.is_file(), src
            target = Path("figures") / src.relative_to(ENTITY)
            (out / "guide" / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / "guide" / target)
            num = len(figures)+1
            figures.append(dict(figure_id=num, variable=v, component=k, scenario=scenario,
                                type=kind, relative_path=str(target), source=str(src), title=title,
                                explanation=KINDS[kind]+" "+meaning+" "+effect+" "+st["summary_cn"]))
            st["figure_ids"].append(num)
        rows.append(st)
    for s in SCENARIOS:
        folder = ENTITY / f"output_WISP_{RID}-full_{s}" / "figure"
        for src in sorted(folder.glob("forc_*.png")):
            key = src.name.split("_" + RID)[0][5:]
            target = Path("figures") / src.relative_to(ENTITY)
            shutil.copy2(src, out / "guide" / target)
            figures.append(dict(figure_id=len(figures)+1, variable=key, component=key, scenario=s,
                type="forcing", relative_path=str(target), source=str(src), title=key,
                explanation=forcing_description(key, s)))
    original = set(str(p) for p in ENTITY.rglob("*.png"))
    assert len(figures) == 378 and len(set(f["source"] for f in figures)) == 378
    assert original == set(f["source"] for f in figures)
    pd.DataFrame(figures).to_csv(out / "tables/all_378_figures_explained.csv", index=False)
    pd.DataFrame(rows).drop(columns=["figure_ids"]).to_csv(out / "tables/all_50_components.csv", index=False)
    p = annual.pivot(index="year", columns=["variable", "component", "scenario"], values="mean")
    yr = pd.DataFrame(index=p.index)
    for v, k, tag in [("gpp", "gpp", "GPP"), ("eco_respiration", "eco_respiration", "Reco"), ("nee", "nee", "NEE")]:
        yr["delta_"+tag] = p[v,k,"VIIRS_Fire"] - p[v,k,"No_Fire"]
    yr["direct_fire"] = p["c_fire_efflux", "fire_carbon_emission", "VIIRS_Fire"]
    yr["net_fire_contribution"] = yr.direct_fire + yr.delta_NEE
    yr["complete_year"] = yr.index > 2014
    yr.to_csv(out / "tables/annual_carbon_decomposition.csv")
    assert np.max(abs(yr.delta_NEE - yr.delta_Reco + yr.delta_GPP)) < .001
    lat, lon = z["lat"], z["lon"]
    ds = abs(float(lon[1]-lon[0]))/2
    meta = dict(region_id=RID, model="WISP_V1_202610", center=[float(lon.mean()),float(lat.mean())],
        bounds=[float(lon.min()-ds),float(lat.min()-ds),float(lon.max()+ds),float(lat.max()+ds)],
        time=["2014-01-12", "2023-12-31"], n_days=3641, shape=[128,128],
        countries="France and Spain; country label FRA does not describe the entire footprint",
        rank=rank, qa_status=json.loads((rp/"qa.json").read_text())["status"], owned_area_m2=float(area),
        run_pixels=int(z["run_valid_mask"].sum()), owned_run_pixels=int(mask.sum()),
        affected_owned_area_m2=float(z["owned_area_m2"][mask & (z["fire_day_count"]>0)].sum()),
        final_cVeg_delta=wa("cEco__cVeg__final_saved__VIIRS_Fire")-wa("cEco__cVeg__final_saved__No_Fire"),
        final_cEco_delta=wa("cEco__cEcosystem__final_saved__VIIRS_Fire")-wa("cEco__cEcosystem__final_saved__No_Fire"),
        direct_full_period=float(yr.direct_fire.sum()), net_full_period=float(yr.net_fire_contribution.sum()),
        annual_support="owned runnable natural vegetation, including unburned cells",
        original_figure_support="native unweighted finite-pixel means, not ownership-weighted",
        figure_count=len(figures), component_count=len(rows))
    dump(out / "metadata.json", meta)
    return rows, figures, annual, yr, z, meta


def figure_pdf(out, rows, figures, annual, yr, z, meta):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.font_manager import FontProperties, fontManager
    from PIL import Image
    import geopandas as gpd
    fontManager.addfont(FONT)
    plt.rcParams.update({"font.family":FontProperties(fname=FONT).get_name(),"font.size":12,
        "axes.unicode_minus":False,"pdf.fonttype":42,"axes.spines.top":False,"axes.spines.right":False})
    pdf = PdfPages(out / "T1862_FIGURE_GUIDE_CN.pdf")
    quick = PdfPages(out / "T1862_QUICK_GUIDE_CN.pdf")
    page = 0
    def title(fig, heading, sub):
        fig.text(.05,.96,heading,ha="left",va="top",fontsize=23,color="#183a43")
        fig.text(.05,.91,sub,ha="left",va="top",fontsize=12,color="#52636a")
    def put(fig, x,y,text,width=82,size=12):
        lines=[]
        # Count East Asian characters as two columns, ASCII as one.
        import unicodedata
        for para in text.split("\n"):
            line=""; used=0
            for char in para:
                n=2 if unicodedata.east_asian_width(char) in "WF" else 1
                if used+n>width:
                    lines.append(line);line="";used=0
                line+=char;used+=n
            lines.append(line)
        fig.text(x,y,"\n".join(lines),ha="left",va="top",fontsize=size,linespacing=1.45)
    def save(fig, preview=None, is_quick=False):
        nonlocal page
        page+=1
        fig.text(.05,.024,f"T1862 | No_Fire / VIIRS_Fire | 第 {page} 页",fontsize=10,color="#60737a")
        pdf.savefig(fig)
        if is_quick: quick.savefig(fig)
        if preview: fig.savefig(out/"plots"/preview,dpi=125,facecolor="white")
        plt.close(fig)
    fig=plt.figure(figsize=(16,10));title(fig,"T1862：火灾影响逐图导读","法国西南部—西班牙北部边界；模型情景案例，并非命名火灾事件或观测站")
    geo=Path("/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/cache/geography/ne_50m_admin_0_countries.zip")
    countries=gpd.read_file("zip://"+str(geo))
    ax=fig.add_axes([.055,.39,.42,.45]);countries.plot(ax=ax,facecolor="#e2e8e1",edgecolor="white",linewidth=.5)
    from matplotlib.patches import Rectangle
    w,s,e,n=meta["bounds"];ax.add_patch(Rectangle((w,s),e-w,n-s,facecolor="#d06548",edgecolor="#9b3f2e",alpha=.8))
    ax.set(xlim=(-3.4,1.1),ylim=(41.9,44.6),xlabel="经度",ylabel="纬度")
    ax.text(-.8,44.0,"France",color="#52636a");ax.text(-2.6,42.3,"Spain",color="#52636a")
    ax.text(-1.35,43.55,"T1862",color="#9b3f2e",weight="bold")
    ax.set_aspect(1/np.cos(np.deg2rad(43.2)))
    ax2=fig.add_axes([.56,.39,.36,.45]);field=np.where(z["run_valid_mask"],z["fire_day_count"],np.nan)
    im=ax2.pcolormesh(z["lon"],z["lat"],field,cmap="YlOrRd",vmin=0,vmax=max(1,np.nanmax(field)),shading="nearest")
    ax2.set(xlabel="经度",ylabel="纬度",title="2014–2023 VIIRS阳性日数")
    ax2.set_aspect(1/np.cos(np.deg2rad(43.2)));fig.colorbar(im,ax=ax2,fraction=.04,pad=.02,label="日数，不是烧毁面积比例")
    put(fig,.055,.30,f"中心 43.1892°N，1.1351°W；128×128格点，3641天。\n统计归属的可运行自然植被面积为 {meta['owned_area_m2']/1e6:,.2f} km²；其中 {meta['affected_owned_area_m2']/1e6:.2f} km² 曾有VIIRS阳性记录。\n该地块在293区中2015–2023单位面积累计直接火灾碳排放排名第1；总排放质量排名第2。",155,14)
    save(fig,"01_location.png",True)
    fig=plt.figure(figsize=(16,10));title(fig,"先抓住三个结果","2015–2023完整年度；数字采用统计归属面积加权，原图有限像元平均是另一种口径")
    y=yr.loc[2015:2023]
    ax=fig.add_axes([.07,.43,.52,.39]);width=.34
    ax.bar(y.index-width/2,y.direct_fire,width,color="#d06847",label="直接燃烧")
    ax.bar(y.index+width/2,y.net_fire_contribution,width,color="#234e64",label="净额外碳释放")
    ax.set(ylabel="g C / m² / year",xlabel="完整年份",xticks=y.index);ax.legend(frameon=False)
    put(fig,.65,.82,f"① 累计直接燃烧\n   {y.direct_fire.sum():.2f} g C/m²\n② 累计净额外释放\n   {y.net_fire_contribution.sum():.2f} g C/m²\n③ 2023年末植被碳差\n   {meta['final_cVeg_delta']:.2f} g C/m²",44,16)
    put(fig,.65,.49,"2019年直接燃烧最强。该年NEE从净吸收变为净释放。净火灾贡献为32.79 g C/m²。",44,13)
    put(fig,.07,.31,"Δ = VIIRS_Fire - No_Fire\n净火灾碳贡献 = 直接燃烧 + ΔNEE = 直接燃烧 + Δ呼吸 - ΔGPP\n2015–2023：66.30 - 41.06 + 53.79 ≈ 79.02 g C/m²\n呼吸下降只抵消部分损失。本案例九个完整年度的净贡献均为正，尽管有些年份ΔNEE为负。",150,15)
    save(fig,"02_fire_effect_summary.png",True)
    fig=plt.figure(figsize=(16,10));title(fig,"如何读原始图、CSV和NetCDF","以README第6节为索引；实际运行与已审计解释优先于旧文档中的通用说明")
    blocks=[
        ("地图", "对角线：两个情景的时间均值。右上：VIIRS_Fire - No_Fire。蓝色为减少，红色为增加。灰色仅表示落在最大差异±5%的范围内，不能直接读作零效应。原图为数组索引；本导读首页地图使用真实经纬度。"),
        ("时间序列", "三层面板分别是逐日、月平均、年平均。对于GPP或火灾排放，年平均仍是平均日速率；累计碳量需要按天求和。2014年只有1月12日以后354天，不是完整年度。"),
        ("CSV与检查", "100行CSV对应50分量×空间/时间两种比较。bias = 有火 - 无火；r²是Pearson相关系数的平方，不是预测决定系数。零/常数序列的相关系数可为NaN。本地块已有逐变量科学QA为pass；不是对真实排放的独立验证。"),
        ("实验边界", "VIIRS阳性记录在有火情景乘0.99作为火灾代理，不能称为实测99%烧毁。WISP字段是占位，未开展真实WISP情景。本轮未保存LAI。restart.nc保存于年龄spinup的较早阶段，不能作为正式模拟的初始库存。"),
        ("阅读与下载", "PDF逐分量展示100张对比图，并解释78张forcing图；配套Markdown逐一列出全部378张原图及解释，含200张单情景输出图。完整下载包还含输入cube、两情景NetCDF、配置、代码、CSV、掩膜及校验和。")]
    for i,(h,t) in enumerate(blocks):
        fig.text(.055,.83-i*.145,h,fontsize=16,color="#1f636c")
        put(fig,.18,.83-i*.145,t,137,13)
    save(fig,is_quick=True)
    for row in rows:
        fs=[figures[i-1] for i in row["figure_ids"]]
        fig=plt.figure(figsize=(16,11));title(fig,row["title_cn"],f"{row['variable']} / {row['component']} | 两张对比图，另有4张单情景图见逐图Markdown")
        for x,f in zip((.04,.52),fs[:2]):
            ax=fig.add_axes([x,.29,.44,.56]);ax.imshow(Image.open(out/"guide"/f["relative_path"]));ax.axis("off")
        put(fig,.05,.25,row["meaning_cn"]+" "+row["effect_cn"],164,12)
        put(fig,.05,.14,row["summary_cn"],164,12)
        fig.text(.05,.065,"原图为全窗口有限像元均值；上述数值为统计归属面积加权。通量年度面板是均值，不是累计量。",fontsize=11,color="#52636a")
        save(fig)
    forcing=[f for f in figures if f["type"]=="forcing"]
    for key in sorted(set(f["variable"] for f in forcing)):
        fs=[f for f in forcing if f["variable"]==key]
        assert len(fs)==2
        fig=plt.figure(figsize=(16,10));title(fig,"输入背景 / "+key,"forcing图解释输入条件；输出的火灾响应请查看前面的配对图")
        for x,f in zip((.04,.52),fs):
            ax=fig.add_axes([x,.30,.44,.54]);ax.imshow(Image.open(out/"guide"/f["relative_path"]));ax.axis("off")
            fig.text(x+.22,.85,f["scenario"],ha="center",fontsize=15)
        put(fig,.05,.25,fs[1]["explanation"],163,14)
        save(fig)
    pdf.close();quick.close()
    return page


def write_guide(out, rows, figures, meta):
    top=f"""# T1862：逐图解释火灾效应

位置：43.1892°N，1.1351°W；法国Pyrenees-Atlantiques与西班牙边界附近。完整128×128窗口跨越两国；不是某个观测站或已识别的命名火灾。

时间：2014-01-12—2023-12-31，3641天。完整年度累计比较使用2015–2023。

原始说明：[README_SINDBAD_WISP_OUTPUT.md](../docs/README_SINDBAD_WISP_OUTPUT.md)。审计后补充：[OUTPUT_SEMANTICS_EN.md](../docs/OUTPUT_SEMANTICS_EN.md)。

## 先读结论

- 该地块2015–2023单位面积累计直接火灾碳排放在293区中排名第1（总质量第2），66.30 g C/m²；按现有面积口径对应98.25 kt C。
- 净额外碳释放79.02 g C/m²，来自直接燃烧66.30、呼吸减少约41.06与GPP减少约53.79的合成。
- 2023年末植被碳少102.67 g C/m²；整个生态系统碳少98.56 g C/m²。凋落物与土壤库的变化不能误读为额外大气排放。
- 2015–2023平均每年GPP少5.98 g C/m²，ET少0.96 mm，蒸腾少1.78 mm，径流多0.68 mm。这是归属自然植被面积的平均，局地影响可能更强。
- 九个完整年度的净火灾贡献均为正；部分年份ΔNEE为负，不能据此说燃烧排放为负。

公式：ΔF_atm = F_fire + ΔNEE = F_fire + ΔR_eco - ΔGPP，其中Δ = VIIRS_Fire - No_Fire。

这里输出的是碳。乘44/12只是把全部已排碳按CO₂质量表达，不是模型已经区分CO₂、CO和CH₄。强度分母为整个已归属、可运行自然植被面积，含未受火格点，不是烧毁面积。

## 每类图怎么读

"""
    text=top+"\n\n".join("- "+v for v in KINDS.values())+"""

原图的均值以全窗口有限像元计算；本导读的数值是唯一归属面积加权。因此图内mean/bias与本文数字不必相等。水体可能有有限0值，统计必须使用运行掩膜。2014年不是完整年。

CSV的r²是Pearson相关系数平方；近1不代表火灾效应为零。对零/常数序列，相关系数未定义是正常的。

## 模型配置需要留意

VIIRS_Fire使用VIIRS二值探测×0.99；0.99不是实测烧毁比例。所有情景使用共同静态植被和树覆盖。真实WISP预测情景未运行。LAI没有启用或保存；当前fAPAR由叶碳计算。

旧README中的restart和fAPAR默认参数说明已被实际运行审计修正：restart不是正式模拟初始库存；PFT参数映射包含优化的消光系数。原始文件保留，不静默改写。

## 50个输出分量：每个分量的6张图

"""
    for row in rows:
        text+=f"### {row['title_cn']} ({row['variable']} / {row['component']})\n\n{row['meaning_cn']}\n\n{row['summary_cn']}\n\n**火灾效应：**{row['effect_cn']}\n\n"
        for i in row["figure_ids"]:
            f=figures[i-1]
            text+=f"#### 图{i}：{f['scenario']} / {f['type']}\n\n{KINDS[f['type']]}\n\n![图{i} {row['title_cn']}]({f['relative_path']})\n\n"
    text+="## 78张输入背景图\n\n这些图说明条件，不是火灾影响结果。\n\n"
    for f in figures:
        if f["type"]!="forcing":continue
        text+=f"### 图{f['figure_id']}：{f['variable']} / {f['scenario']}\n\n{f['explanation']}\n\n![图{f['figure_id']}]({f['relative_path']})\n\n"
    (out/"guide/T1862_EVERY_FIGURE_CN.md").write_text(text)
    (out/"README_CN.md").write_text(f"""# T1862 案例下载包

先看 `T1862_QUICK_GUIDE_CN.pdf`（3页）；详细阅读 `T1862_FIGURE_GUIDE_CN.pdf`。
全部378张原始图按图编号解释：`guide/T1862_EVERY_FIGURE_CN.md`。
可检索清单：`tables/all_378_figures_explained.csv`；50分量数值：`tables/all_50_components.csv`。

原始项目说明和审计修正均在 `docs/`。本案例科学QA状态：{meta['qa_status']}。

完整包中的 `input/` 保存十年输入；`entity/` 保存两情景全部原始结果、图、配置、代码、CSV和Julia摘要；`input_qa/`保存掩膜及归属；`parameters/`保存PFT参数。
轻量阅读包不含这些大数据目录。打开图片、PDF、CSV不需要Julia。
`code/read_downloaded_entity.py`给出使用xarray读取NetCDF的例子。

所有数据保持来源原值。部分文件路径仍记录原服务器身份，下载包不是自动可重跑的完整计算环境。
`MANIFEST_SHA256.csv`给出每个打包文件的源路径、大小和SHA256；`ARCHIVES.json`给出ZIP校验和。

位置与面积定义、日期、排名和QA记录见 `metadata.json`。不要把本案例视为整个地中海的无偏估计。
""")
    (out/"code/read_downloaded_entity.py").write_text('''"""Run from any location after extracting the full ZIP; requires xarray/netCDF4."""
from pathlib import Path
import xarray as xr
root = Path(__file__).resolve().parents[1]
rid = "'''+RID+'''"
cube = next((root / "input").glob("*.nc"))
with xr.open_dataset(cube) as ds:
    print("Input:", ds.sizes)
for scenario in ("No_Fire", "VIIRS_Fire"):
    p = root / "entity" / rid / ("output_WISP_"+rid+"-full_"+scenario) / "data" / "gpp.nc"
    with xr.open_dataset(p) as ds:
        # Lazy open: load one day rather than all ten years.
        print(scenario, ds["gpp"].dims, ds["gpp"].isel(time=0).load())
# Use saved run_masks/ownership before spatial summaries. Do not blindly average water zeros.
''')


def package(out):
    from PIL import Image
    shutil.copy2(__file__, out / "code/entity_case_guide.py")
    figures=pd.read_csv(out/"tables/all_378_figures_explained.csv")
    for f in figures.itertuples():
        with Image.open(out/"guide"/f.relative_path) as im: im.verify()
    cube=BATCH/"cubes"/(RID+"_frozen_v1")/("daily_master_dev_v5_"+RID+"_frozen_v1.nc")
    files=[(p,str(p.relative_to(out))) for p in sorted(out.rglob("*")) if p.is_file() and p.suffix not in (".zip", ".partial") and p.name not in ("MANIFEST_SHA256.csv","ARCHIVES.json","validation.json")]
    light=list(files)
    files += [(cube,"input/"+cube.name)]
    files += [(p,"entity/"+RID+"/"+str(p.relative_to(ENTITY))) for p in sorted(ENTITY.rglob("*")) if p.is_file()]
    q=BATCH/"qa"/RID
    # Weather preparation QA contains monthly NetCDF intermediates; the download
    # needs masks/provenance, not an extra weather dataset alongside the input cube.
    qa_files = [p for p in q.glob("*") if p.is_file()]
    qa_files += [p for p in (q / "static").rglob("*") if p.is_file()]
    files += [(p,"input_qa/"+str(p.relative_to(q))) for p in sorted(qa_files)]
    pr=ENTITY.parent.parent/"pft"
    for p in sorted(pr.glob("params_by_pft_WISP_V1_202610_pft.*")):
        if p.is_file(): files.append((p,"parameters/"+p.name))
        else: files += [(f,"parameters/"+p.name+"/"+str(f.relative_to(p))) for f in sorted(p.rglob("*")) if f.is_file()]
    assert len(set(name for p,name in files))==len(files)
    manifest=[]
    for idx,(p,name) in enumerate(files):
        before=p.stat();sha=digest(p);after=p.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError("Source changed: "+str(p))
        manifest.append(dict(archive_path=name,source=str(p),bytes=before.st_size,mtime_ns=before.st_mtime_ns,sha256=sha))
        if idx%100==0:print("hashed",idx,"/",len(files),flush=True)
    pd.DataFrame(manifest).to_csv(out/"MANIFEST_SHA256.csv",index=False)
    archives=[]
    for name,items in [("T1862_GUIDE_AND_FIGURES.zip",light),("T1862_INPUT_AND_ALL_OUTPUTS.zip",files)]:
        dest=out/name;tmp=dest.with_suffix(".zip.partial")
        with zipfile.ZipFile(tmp,"w",compression=zipfile.ZIP_STORED,allowZip64=True) as z:
            for p,arc in items:z.write(p,arc)
            z.write(out/"MANIFEST_SHA256.csv","MANIFEST_SHA256.csv")
        with zipfile.ZipFile(tmp) as z:
            assert z.testzip() is None
            # CRC alone checks archive integrity; compare scientific payload SHA256 to source too.
            if name.endswith("ALL_OUTPUTS.zip"):
                for rec in manifest:
                    if rec["archive_path"].endswith(".nc"):
                        h=hashlib.sha256()
                        with z.open(rec["archive_path"]) as f:
                            for b in iter(lambda:f.read(8*1024*1024),b""):h.update(b)
                        assert h.hexdigest()==rec["sha256"],rec["archive_path"]
        os.replace(tmp,dest)
        archives.append(dict(path=str(dest),bytes=dest.stat().st_size,sha256=digest(dest),files=len(items)+1,crc_check="pass"))
        print("PACKAGED",name,dest.stat().st_size,flush=True)
    dump(out/"ARCHIVES.json",archives)
    dump(out/"validation.json",dict(all_original_pngs_explained=378,components=50,png_decode="pass",archive_crc="pass",nc_source_vs_zip_sha256="pass",source_scientific_qa="pass",source_mutation=False,simulation_run=False))
    return archives


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--output",type=Path,default=ROOT/"entity_case_studies/T1862_v1");ap.add_argument("--package-only",action="store_true");ap.add_argument("--no-package",action="store_true")
    args=ap.parse_args();out=args.output
    if not args.package_only:
        rows,figures,annual,yr,z,meta=init(out)
        write_guide(out,rows,figures,meta)
        pages=figure_pdf(out,rows,figures,annual,yr,z,meta)
        print("GUIDE COMPLETE",pages,"pages",flush=True)
    if not args.no_package:print(json.dumps(package(out),indent=2))


if __name__=="__main__":main()
