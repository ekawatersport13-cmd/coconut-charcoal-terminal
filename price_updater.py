#!/usr/bin/env python3
"""
Coconut Shell Charcoal Price Updater
每日价格更新脚本 — 优先抓取真实市场数据，失败则模拟

数据源（按优先级）：
1. 生意社 100ppi.com  — 中国椰壳活性炭每日出厂价（真实）
2. SMM 上海有色金属网 — 进口量/均价（月更，真实）
3. ExchangeRate-API    — USD/IDR, USD/CNY 实时汇率（真实）
4. 模拟数据            — 上述全部失败时 fallback

用法：
  python price_updater.py            # 正常更新（跳过重复日）
  python price_updater.py --force  # 强制覆盖今日数据
  python price_updater.py --dry-run # 预览不写入
  python price_updater.py --web     # 强制尝试网页抓取（即使已有今日数据）
"""

import json
import re
import random
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

# ── 文件路径 ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
PRICES_JS  = SCRIPT_DIR / "prices.js"

# ── 波动参数（仅 fallback 模拟时使用）─────────────────────────────────
VOLATILITY = {
    "raw": 25, "bbq": 30, "shisha": 40,
    "ac800": 35, "ac1000": 40, "usd_idr": 15,
    "usd_cny": 0.005, "freight": 10,
}
TREND = {
    "raw": 5, "bbq": 5, "shisha": 8,
    "ac800": 5, "ac1000": 4,
    "usd_idr": 2, "usd_cny": 0.0003, "freight": 1,
}
SEASONAL = {1:0.95,2:0.96,3:1.02,4:1.05,5:1.08,6:1.10,
             7:1.12,8:1.10,9:1.05,10:1.03,11:0.98,12:0.95}

# ── 解析 / 写入 prices.js ────────────────────────────────────────────────
def parse_prices_js(fp: Path) -> dict:
    content = fp.read_text(encoding="utf-8")
    m = re.search(r"var priceDatabase\s*=\s*(\{.*?\});", content, re.DOTALL)
    if not m:
        raise ValueError("Cannot find priceDatabase in prices.js")
    return json.loads(m.group(1))

def write_prices_js(fp: Path, data: dict):
    fp.write_text(f"var priceDatabase = {json.dumps(data, ensure_ascii=False, indent=2)};",
                  encoding="utf-8")

# ── 网页抓取函数 ────────────────────────────────────────────────────────
def fetch_url(url: str, timeout: int = 15) -> str | None:
    """用 urllib 抓取网页，返回 HTML 字符串或 None。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [FETCH] 失败 {url[:50]}: {e}")
        return None


def fetch_100ppi_prices() -> list[dict] | None:
    """
    抓取生意社椰壳活性炭报价。
    返回 [{"date": "...", "type": "活性炭800", "price": 6950, "unit": "¥/吨"}, ...]
    """
    url = "https://www.100ppi.com/news/detail-20260526-5703487.html"
    html = fetch_url(url)
    if not html:
        # 尝试列表页
        html = fetch_url("https://www.100ppi.com/mprice/plist-1-3743-1.html")
    if not html:
        return None

    results = []
    # 用正则从 HTML 里提取价格数字 + 碘值描述
    # 模式: 数字 + 元/吨 或 ¥/吨
    try:
        # 提取所有价格数字（简略解析，实际根据页面结构微调）
        price_pattern = r"(\d{4,5})\s*元/吨"
        prices_found = re.findall(price_pattern, html)
        if prices_found:
            print(f"  [100PPI] 找到 {len(prices_found)} 个价格: {prices_found[:5]}")
            # 简单映射：取中位数作为 ac800 参考价
            vals = sorted([int(p) for p in prices_found if 2000 < int(p) < 20000])
            if vals:
                return [{
                    "type": "活性炭800",
                    "price": vals[len(vals)//2],
                    "unit": "¥/吨",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "source": "100ppi"
                }]
    except Exception as e:
        print(f"  [100PPI] 解析失败: {e}")
    return None


def fetch_chinabgao_prices() -> list[dict] | None:
    """抓取 chinabgao.com 椰壳活性炭实时报价。"""
    url = "https://www.chinabgao.com/jiage/ykhxt/"
    html = fetch_url(url)
    if not html:
        return None
    try:
        price_pattern = r"(\d{4,5})\s*元/吨"
        prices_found = re.findall(price_pattern, html)
        if prices_found:
            vals = sorted([int(p) for p in prices_found if 2000 < int(p) < 20000])
            if vals:
                print(f"  [CHINABGAO] 找到 {len(vals)} 个价格")
                return [{
                    "type": "活性炭汇总",
                    "price": vals[len(vals)//2],
                    "unit": "¥/吨",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "source": "chinabgao"
                }]
    except Exception as e:
        print(f"  [CHINABGAO] 解析失败: {e}")
    return None


def fetch_forex_rates() -> dict | None:
    """从免费 API 获取真实汇率。"""
    try:
        import urllib.request
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            rates = data.get("rates", {})
            result = {}
            if "CNY" in rates:
                result["usd_cny"] = round(rates["CNY"], 2)
            if "IDR" in rates:
                result["usd_idr"] = round(rates["IDR"])
            if result:
                print(f"  [FOREX] 实时汇率: USD/CNY={result.get('usd_cny')}, USD/IDR={result.get('usd_idr')}")
                return result
    except Exception as e:
        print(f"  [FOREX] 获取失败: {e}")
    return None


def fetch_smm_import_data() -> dict | None:
    """
    抓取 SMM 椰壳炭进口数据（月更）。
    返回 {"import_price_usd": 926.69, "import_volume_ton": 25068.8, ...}
    """
    url = "http://goodsfu.10jqka.com.cn/20260520/c676836568.shtml"
    html = fetch_url(url)
    if not html:
        return None
    try:
        # 提取进口均价数字
        m = re.search(r"(\d{3,4}\.\d{2})\s*美元/吨", html)
        price_usd = float(m.group(1)) if m else None
        # 提取进口量
        m2 = re.search(r"(\d{4,6}\.\d)\s*吨", html)
        volume = float(m2.group(1)) if m2 else None
        if price_usd:
            print(f"  [SMM] 进口均价: ${price_usd}/吨, 进口量: {volume}吨")
            return {"import_price_usd": price_usd, "import_volume_ton": volume}
    except Exception as e:
        print(f"  [SMM] 解析失败: {e}")
    return None


# ── 模拟数据生成（fallback）────────────────────────────────────────────
def generate_simulated(last: dict, today: str) -> dict:
    month = datetime.strptime(today, "%Y-%m-%d").month
    season = SEASONAL.get(month, 1.0)
    random.seed(today)
    rec = {"date": today, "note": "模拟数据"}
    for field in ["raw","bbq","shisha","ac800","ac1000","usd_idr","usd_cny","freight"]:
        base = last.get(field, 0)
        vol  = VOLATILITY.get(field, 20)
        trend = TREND.get(field, 0) * season
        chg  = trend + random.gauss(0, vol)
        if field == "usd_cny":
            v = round(max(6.5, min(8.0, base + chg)), 2)
        elif field == "freight":
            v = max(500, min(3000, round(base + chg)))
        elif field == "raw":
            v = max(2500, min(8000, round(base + chg)))
        elif field == "bbq":
            v = max(2000, min(12000, round(base + chg)))
        elif field == "shisha":
            v = max(3000, min(15000, round(base + chg)))
        elif field in ("ac800","ac1000"):
            v = max(4000, min(20000, round(base + chg)))
        elif field == "usd_idr":
            v = max(14000, min(18000, round(base + chg)))
        else:
            v = max(0, round(base + chg))
        rec[field] = v
    return rec


# ── 主更新逻辑 ─────────────────────────────────────────────────────────
def build_today_record(last: dict, force_web: bool = False) -> dict:
    """
    优先抓取真实数据；抓不到的字段用模拟补足。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n🔍 尝试获取真实市场数据...")

    # 初始化记录（复制昨日价格作为基准）
    record = {"date": today, "note": "自动采集"}
    for field in ["raw","bbq","shisha","ac800","ac1000","usd_idr","usd_cny","freight"]:
        record[field] = last.get(field, 0)

    success_count = 0

    # 1. 汇率（最容易成功，优先）
    forex = fetch_forex_rates()
    if forex:
        for k, v in forex.items():
            record[k] = v
        success_count += 1

    # 2. 生意社中国活性炭价格
    p100 = fetch_100ppi_prices()
    if p100:
        # 用抓取到的价格反推各品类
        # 简单策略：以抓取价为基准，推算其他品类
        base_ac = p100[0]["price"]
        record["ac800"]  = base_ac
        record["ac1000"] = round(base_ac * 1.32)
        record["shisha"] = round(base_ac * 0.91)
        record["bbq"]    = round(base_ac * 0.56)
        record["note"] = "网页抓取+推算"
        success_count += 1
        print(f"  ✓ 中国下游价格已更新 (ac800={base_ac})")

    # 3. SMM 进口数据
    smm = fetch_smm_import_data()
    if smm and smm.get("import_price_usd"):
        # 进口均价 USD/吨 → 推算印尼离岸价 IDR/kg
        import_usd = smm["import_price_usd"]
        # CIF价 → FOB价（减去运费和保险，约减$150-200）
        fob_usd = import_usd - 180
        raw_idr = round(fob_usd * record["usd_idr"] / 1000)  # IDR/kg
        record["raw"] = max(2500, min(8000, raw_idr))
        record["note"] = (record.get("note") or "SMM导入") if record.get("note")=="网页抓取+推算" else "SMM导入"
        success_count += 1
        print(f"  ✓ 进口均价已更新 (CIF=${import_usd}, FOB≈{record['raw']} IDR/kg)")

    # 4. 如果真实数据太少，用模拟补足剩余字段
    if success_count < 2:
        print(f"  ⚠ 真实数据不足 ({success_count}/3)，启用模拟补足...")
        simulated = generate_simulated(last, today)
        for field in ["raw","bbq","shisha","ac800","ac1000","freight"]:
            if record[field] == last.get(field, 0) and field != "usd_cny":
                record[field] = simulated[field]
        if record["note"] == "自动采集":
            record["note"] = "模拟数据"
        print(f"  ✓ 模拟数据已补足")
    else:
        # 即使有真实数据，也对没抓到的字段做小幅模拟调整
        simulated = generate_simulated(last, today)
        for field in ["freight"]:  # 运费暂时无公开API，用模拟
            record[field] = simulated[field]
        print(f"  ✓ 混合模式: 真实{success_count}项 + 模拟补足")

    return record


def main():
    dry_run  = "--dry-run"  in sys.argv
    force    = "--force"    in sys.argv
    force_web = "--web"      in sys.argv or "--force-web" in sys.argv

    print(f"=== 椰壳炭行情更新 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'DRY-RUN' if dry_run else 'FORCE' if force else 'NORMAL'}")

    if not PRICES_JS.exists():
        print(f"❌ 找不到 {PRICES_JS}")
        sys.exit(1)

    data    = parse_prices_js(PRICES_JS)
    history = data.get("history", [])
    if not history:
        print("❌ prices.js 中无历史数据")
        sys.exit(1)

    last  = history[-1]
    today = datetime.now().strftime("%Y-%m-%d")

    # 检查今日是否已存在
    existing = [r for r in history if r["date"] == today]
    if existing and not force and not force_web:
        print(f"⚠ 今日 {today} 已有数据，跳过 (用 --force 强制覆盖)")
        print(f"   现有: {existing[0]}")
        sys.exit(0)

    # 生成/抓取今日数据
    if force_web:
        print(f"\n🌐 --force-web 模式: 强制重新抓取网页...")
        record = build_today_record(last, force_web=True)
    else:
        record = build_today_record(last, force_web=False)

    # 打印涨跌情况
    print(f"\n📊 今日行情 ({today}):")
    for key, label, unit in [
        ("raw","炭化料","IDR/kg"), ("bbq","烧烤炭","¥/吨"),
        ("shisha","水烟炭","¥/吨"), ("ac800","活性炭800","¥/吨"),
        ("ac1000","活性炭1000","¥/吨"), ("usd_idr","USD/IDR","IDR"),
        ("usd_cny","USD/CNY",""), ("freight","海运费","USD")]:
        v  = record.get(key, 0)
        ov = last.get(key, 0)
        chg = v - ov
        arrow = "▲" if chg >= 0 else "▼"
        print(f"  {label:>8} {v:>8,} {unit:>8}  {arrow}{abs(chg):.1f}")

    if dry_run:
        print("\n[Dry-run] 未写入文件")
        return

    # 写入
    if force or force_web:
        history = [r for r in history if r["date"] != today]
        data["history"] = history

    data["history"].append(record)
    data["updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08")
    data["source"] = record.get("note", "auto")
    write_prices_js(PRICES_JS, data)
    print(f"\n✅ 已更新 prices.js ({len(data['history'])} 天数据)")
    print(f"   文件: {PRICES_JS}")


if __name__ == "__main__":
    main()
