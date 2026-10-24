#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金定投日报 v4 —— 估值驱动 + 总仓位 + 汇率 + 资金规则
- 抓取持仓基金最新净值 / 日涨跌
- 红利低波用「股息率 + PB」双锚判断；宽基用 PE 历史百分位判断
- 总仓位概览（权益 / 稳健 / 黄金），权益占比过高减半、过低加仓
- QDII 汇率备注（人民币近 3 年极端升值/贬值时提示换汇成本）
- 低估资金来源与上限（月结余挤 500/1000、拨理财 2~3 万、连续 3 月翻倍）
- 通过 QQ 邮箱每日发送 HTML 报告

数据源：天天基金（净值）+ 蛋卷（估值）+ frankfurter（汇率），仅供个人参考，不构成投资建议。
"""

import bisect
import datetime
import html as html_mod
import json
import os
import smtplib
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

try:
    from zoneinfo import ZoneInfo

    def now_cn():
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))

except ImportError:  # Python < 3.9
    def now_cn():
        return datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 颜色语义
RED = "#c62828"
GREEN = "#2e7d32"
ORANGE = "#e65100"
BLUE = "#1565c0"
GRAY = "#616161"
MUTED = "#90a4ae"


# ---------------------------------------------------------------------------
# 配置与状态
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    default = {"valuation_tier": {}, "deep_low": {}, "holdings": {}}
    if not os.path.exists(STATE_PATH):
        return default
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default
    data.setdefault("valuation_tier", {})
    data.setdefault("deep_low", {})
    data.setdefault("holdings", {})
    return data


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_dca(schedule, dt):
    """判断今天是否为该基金的定投日。dt 为北京时间 datetime。"""
    if not schedule:
        return False
    t = schedule.get("type")
    weekday = dt.weekday()  # 周一=0 … 周日=6
    if t == "daily":
        return weekday < 5  # 仅交易日（周一~周五）
    if t == "weekly":
        return weekday == schedule.get("weekday", 3)
    if t == "monthly":
        return dt.day == schedule.get("day", 1)
    return False


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------

def fetch_lsjz(code):
    """天天基金历史净值接口：拿最新净值、净值日期、真实日涨跌幅。"""
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    headers = dict(UA)
    headers["Referer"] = "https://fundf10.eastmoney.com/"
    r = requests.get(
        url,
        params={"fundCode": code, "pageIndex": 1, "pageSize": 2},
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()
    items = (r.json().get("Data") or {}).get("LSJZList") or []
    return items


def get_fund_snapshot(fund):
    """返回单只场外基金的当前快照。"""
    code = fund["code"]
    nav = nav_date = day_change = None
    try:
        items = fetch_lsjz(code)
        if items:
            nav = float(items[0]["DWJZ"])
            nav_date = items[0]["FSRQ"]
            day_change = float(items[0]["JZZZL"])
    except Exception as e:
        print(f"[warn] 抓取 {code} 净值失败: {e}", file=sys.stderr)
    return {
        "code": code,
        "name": fund.get("name") or code,
        "nav": nav,
        "nav_date": nav_date,
        "day_change": day_change,
    }


def fetch_valuations():
    """蛋卷估值接口：一次抓全部指数，返回 {index_code: {...}}。"""
    url = "https://danjuanfunds.com/djapi/index_eva/dj"
    headers = dict(UA)
    headers["Referer"] = "https://danjuanfunds.com/"
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("items") or []
    result = {}
    for it in items:
        code = it.get("index_code")
        pe = it.get("pe") or 0
        pe_pct = it.get("pe_percentile")
        pb_pct = it.get("pb_percentile")
        pct = pe_pct if (pe and pe > 0 and pe_pct is not None) else pb_pct
        result[code] = {
            "name": it.get("name") or code,
            "pe": pe,
            "pb": it.get("pb"),
            "yeild": it.get("yeild"),
            "pe_percentile": pe_pct,
            "pb_percentile": pb_pct,
            "percentile": pct,
            "date": it.get("date") or "",
        }
    return result


def fetch_fx_extreme(years=3, extreme_pct=0.05):
    """近 years 年 USD/CNY 历史，算最新汇率所在分位。返回 dict 或 None。"""
    end = now_cn().date()
    start = end - datetime.timedelta(days=int(years * 365))
    url = (
        f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
        "?base=USD&symbols=CNY"
    )
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
        rates = r.json().get("rates") or {}
        pairs = [(k, v["CNY"]) for k, v in rates.items() if v.get("CNY")]
        if not pairs:
            return None
        pairs.sort()
        values = [v for _, v in pairs]
        latest = pairs[-1][1]
        pos = bisect.bisect_left(values, latest) / len(values)
        return {
            "latest": latest,
            "pos": pos,
            "min": values[0],
            "max": values[-1],
            "date": pairs[-1][0],
            "extreme_pct": extreme_pct,
        }
    except Exception as e:
        print(f"[warn] 汇率获取失败: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 估值判断
# ---------------------------------------------------------------------------

def valuation_percentile(pct):
    """PE 历史百分位 → (档位, 动作)。"""
    if pct is None:
        return "未知", "无法判断"
    if pct < 0.20:
        return "深度低估", "加倍定投 + 可一次性买入"
    if pct < 0.30:
        return "低估", "加倍定投"
    if pct < 0.70:
        return "正常", "正常定投"
    if pct < 0.80:
        return "偏高", "暂停定投"
    return "高估", "暂停定投 + 分批止盈"


def valuation_dividend_pb(yld, pb, s):
    """股息率 + PB 双锚 → (档位, 指标文本)。任一触发原则。"""
    # 股息率档
    if yld is None:
        yb = "未知"
    elif yld > s["buy_when_yield"]:
        yb = "低估"
    elif yld >= s["normal_yield_low"]:
        yb = "正常"
    elif yld >= s["high_yield"]:
        yb = "偏高"
    else:
        yb = "高估"

    # PB 档
    if pb is None:
        pb_ = "未知"
    elif pb < s["buy_pb"]:
        pb_ = "低估"
    elif pb <= s["normal_pb_high"]:
        pb_ = "正常"
    elif pb <= s["high_pb"]:
        pb_ = "偏高"
    else:
        pb_ = "高估"

    # 深度低估：股息率很高 且 PB 很低
    if (
        yld is not None and pb is not None
        and yld > s["deep_low_yield"] and pb < s["deep_low_pb"]
    ):
        tier = "深度低估"
    else:
        bands = [yb, pb_]
        if "高估" in bands:
            tier = "高估"
        elif "偏高" in bands:
            tier = "偏高"
        elif "低估" in bands:
            tier = "低估"
        else:
            tier = "正常"

    yld_s = "—" if yld is None else f"{yld * 100:.2f}%"
    pb_s = "—" if pb is None else f"{pb:.3f}"
    return tier, f"股息{yld_s} · PB{pb_s}"


def tier_action(tier):
    """档位 → 操作文案。"""
    return {
        "深度低估": "加倍定投 + 可一次性买入",
        "低估": "加倍定投",
        "正常": "正常定投",
        "偏高": "暂停定投",
        "高估": "暂停定投",
    }.get(tier, "无法判断")


def funding_text(tier, strat):
    """需求4：低估时的资金来源与上限。"""
    f = strat.get("funding", {})
    if tier == "深度低估":
        return (
            f"从月结余额外挤 {f.get('deep_low_add', 1000)} 元，"
            f"并从稳健理财拨 {f.get('deep_low_lump_low', 20000)//10000}~"
            f"{f.get('deep_low_lump_high', 30000)//10000} 万一次性买入"
        )
    if tier == "低估":
        return f"从月结余额外挤 {f.get('low_add', 500)} 元加倍定投，不动用理财"
    if tier == "正常":
        return "正常定投"
    return "暂停定投"


def track_deep_low(state, code, is_deep_low, cur_month):
    """跟踪某基金「深度低估」的连续自然月数。返回当前连续月数。"""
    state.setdefault("deep_low", {})
    if not is_deep_low:
        state["deep_low"].pop(code, None)
        return 0
    entry = state["deep_low"].get(code)
    if entry is None:
        count = 1
    elif entry["month"] == cur_month:
        count = entry["count"]
    else:
        count = entry["count"] + 1
    state["deep_low"][code] = {"month": cur_month, "count": count}
    return count


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def pct(x):
    return f"{x * 100:+.2f}%"


def pct0(x):
    return f"{x * 100:.1f}%"


def money(x):
    return f"{x:,.0f}"


def esc(s):
    return html_mod.escape(str(s))


def sign_color(v):
    if v is None:
        return GRAY
    if v > 0:
        return RED
    if v < 0:
        return GREEN
    return GRAY


def tier_style(tier):
    return {
        "深度低估": ("#e8f5e9", "#1b5e20"),
        "低估": ("#e8f5e9", GREEN),
        "正常": ("#eceff1", "#455a64"),
        "偏高": ("#fff3e0", ORANGE),
        "高估": ("#ffebee", RED),
    }.get(tier, ("#eceff1", MUTED))


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

def build_report(cfg, state):
    strat = cfg["strategy"]
    portfolio = cfg.get("portfolio", {})

    valuations = {}
    try:
        valuations = fetch_valuations()
    except Exception as e:
        print(f"[warn] 抓取估值失败: {e}", file=sys.stderr)

    fx = fetch_fx_extreme(
        years=strat.get("fx", {}).get("years", 3),
        extreme_pct=strat.get("fx", {}).get("extreme_pct", 0.05),
    )

    dt = now_cn()
    cur_month = dt.strftime("%Y-%m")
    today_str = dt.strftime("%Y-%m-%d")

    rows = []
    buy_list, sell_list, hold_list = [], [], []
    dip_list, change_list, errors = [], [], []
    equity_total = 0.0

    for fund in cfg["funds"]:
        code = fund["code"]
        name = fund.get("name") or code
        dca = f"{fund.get('dca_frequency', '')} {fund.get('per_period', 0)}元"
        method = fund.get("valuation_method", "percentile")

        # ---- 估值 ----
        v_code = fund.get("valuation_index")
        v = valuations.get(v_code) if v_code else None
        if method == "dividend_pb":
            tier, val_detail = valuation_dividend_pb(
                v.get("yeild") if v else None,
                v.get("pb") if v else None,
                strat.get("dividend_pb", {}),
            )
            val_short = tier
        else:
            p = v.get("percentile") if v else None
            tier, _ = valuation_percentile(p)
            val_short = f"{tier} {pct0(p)}" if p is not None else "—"
            val_detail = f"PE百分位 {pct0(p)}" if p is not None else "无估值"

        # 档位变化检测
        prev_tier = state["valuation_tier"].get(code)
        state["valuation_tier"][code] = tier
        if prev_tier and prev_tier != tier:
            change_list.append((name, prev_tier, tier))

        # ---- 深度低估连续月跟踪（需求4）----
        is_deep_low = tier == "深度低估"
        deep_months = track_deep_low(state, code, is_deep_low, cur_month)
        deep_3m = deep_months >= strat.get("funding", {}).get("deep_low_months", 3)

        # ---- 持仓（state 持久化，按定投计划自动累加）----
        per_period = fund.get("per_period", 0) or 0
        h = state["holdings"].get(code)
        if h is None:
            if fund.get("manual"):
                h = {
                    "amount": fund.get("amount", 0),
                    "cost": fund.get("cost_amount", 0),
                    "last_dca": None,
                }
            else:
                shares0 = fund.get("shares", 0) or 0
                cost0 = (fund.get("cost_price") or 0) * shares0
                h = {"shares": shares0, "cost": cost0, "last_dca": None}
            state["holdings"][code] = h

        # 是否今天定投且估值允许（偏高/高估 → 暂停定投，不累加）
        allow_dca = tier not in ("偏高", "高估", "未知")
        is_dca_day = should_dca(fund.get("dca_schedule"), dt)

        # ---- 金额与市值 ----
        if fund.get("manual"):
            if is_dca_day and allow_dca and h.get("last_dca") != today_str:
                h["cost"] += per_period
                h["last_dca"] = today_str
            amount = h.get("amount", 0)
            cost_amount = h.get("cost", 0)
            if not cost_amount or cost_amount <= 0:
                errors.append(f"{name}({code}) 缺少持仓成本")
                continue
            ret = amount / cost_amount - 1.0
            pnl = amount - cost_amount
            ret_str = pct(ret)
            pnl_str = f"{pnl:+,.0f}"
            mkt_value = amount
            nav_display = f"{money(amount)} 元"
            nav_sub = f"成本 {money(cost_amount)}"
        else:
            snap = get_fund_snapshot(fund)
            if snap["nav"] is None:
                errors.append(f"{name}({code}) 数据获取失败")
                continue
            nav = snap["nav"]
            nav_date = snap["nav_date"] or "?"
            day_change = snap["day_change"]
            day_str = "—" if day_change is None else f"{day_change:+.2f}%"

            # 定投累加：用最近净值折算份额
            if is_dca_day and allow_dca and h.get("last_dca") != today_str:
                h["shares"] += per_period / nav
                h["cost"] += per_period
                h["last_dca"] = today_str

            shares = h.get("shares", 0)
            cost_total = h.get("cost", 0)
            cost_price = cost_total / shares if shares > 0 else None

            ret = pnl = None
            ret_str = pnl_str = "—"
            mkt_value = 0.0
            if cost_price:
                ret = nav / cost_price - 1.0
                pnl = (nav - cost_price) * shares
                mkt_value = nav * shares
                ret_str = pct(ret)
                pnl_str = f"{pnl:+,.0f}"

            nav_display = f"{nav:.4f}"
            nav_sub = nav_date
            if day_change is not None:
                nav_sub += f" · 日 {day_str}"

            if day_change is not None and day_change / 100.0 <= strat.get("dip_alert_pct", -0.02):
                dip_list.append((name, day_str))

        equity_total += mkt_value

        rows.append({
            "name": name, "code": code, "dca": dca, "method": method,
            "nav_display": nav_display, "nav_sub": nav_sub,
            "ret_val": ret, "ret_str": ret_str,
            "pnl_val": pnl, "pnl_str": pnl_str,
            "tier": tier, "val_short": val_short, "val_detail": val_detail,
            "deep_months": deep_months, "deep_3m": deep_3m,
        })

        # ---- 操作建议分类 ----
        if tier in ("偏高", "高估"):
            sell_list.append((name, tier, tier_action(tier)))
        elif tier in ("深度低估", "低估"):
            buy_list.append((name, tier, funding_text(tier, strat), deep_months))
        elif tier == "正常":
            hold_list.append((name, "正常定投"))

    # ================= 总仓位 =================
    stable = portfolio.get("stable_amount", 0) or 0
    gold = portfolio.get("gold_amount", 0) or 0
    monthly_surplus = portfolio.get("monthly_surplus", 0) or 0
    monthly_dca = portfolio.get("monthly_dca", 0) or 0
    total_assets = equity_total + stable + gold
    equity_pct_val = equity_total / total_assets if total_assets > 0 else None

    pos_alert = None  # 仓位提示
    pos_coef = 1.0    # 定投金额系数（减半用）
    if total_assets > 0 and stable + gold > 0:
        if equity_pct_val > strat.get("portfolio", {}).get("equity_high_pct", 0.60):
            pos_alert = ("high", equity_pct_val)
            pos_coef = 0.5
        elif equity_pct_val < strat.get("portfolio", {}).get("equity_low_pct", 0.25):
            pos_alert = ("low", equity_pct_val)
    elif stable + gold == 0:
        pos_alert = ("incomplete", None)

    # ================= 渲染 HTML =================
    date_str = dt.strftime("%Y-%m-%d")
    weekday = WEEKDAY_CN[dt.weekday()]

    h = []
    h.append(
        '<div style="max-width:720px;margin:0 auto;font-family:-apple-system,'
        "BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',"
        "sans-serif;color:#333;font-size:14px;line-height:1.6;background:#fff;"
        'padding:8px 4px">'
    )
    h.append(
        f'<div style="font-size:20px;font-weight:700;color:#2c3e50;margin-bottom:2px">'
        f"定投日报 · {date_str} {weekday}</div>"
    )
    h.append(
        '<div style="color:#999;font-size:12px;margin-bottom:16px">'
        "净值取最近已收盘交易日；估值：红利看股息率+PB，宽基看 PE 百分位</div>"
    )

    # ---- 一、持仓概览 ----
    h.append(_section("持仓概览"))
    h.append(
        '<table style="width:100%;border-collapse:collapse;font-size:13px" '
        'cellpadding="0" cellspacing="0">'
    )
    headers = ["基金", "净值 / 金额", "收益率", "浮盈亏(元)", "估值", "今日建议"]
    h.append('<tr style="background:#f5f7fa">')
    for th in headers:
        h.append(
            f'<th style="padding:8px;text-align:left;color:#7a7f86;font-size:12px;'
            f'font-weight:600;border-bottom:2px solid #e5e8ec">{th}</th>'
        )
    h.append("</tr>")

    for r in rows:
        bg, fg = tier_style(r["tier"])
        fund_td = (
            f'<div style="font-weight:600;color:#333">{esc(r["name"])}</div>'
            f'<div style="color:#999;font-size:12px">{esc(r["code"])} · {esc(r["dca"])}</div>'
        )
        nav_td = (
            f'<div>{esc(r["nav_display"])}</div>'
            f'<div style="color:#999;font-size:12px">{esc(r["nav_sub"])}</div>'
        )
        ret_td = f'<span style="color:{sign_color(r["ret_val"])};font-weight:600">{esc(r["ret_str"])}</span>'
        pnl_td = f'<span style="color:{sign_color(r["pnl_val"])}">{esc(r["pnl_str"])}</span>'

        if r["tier"] == "未知":
            val_td = f'<span style="color:{MUTED}">—</span>'
        else:
            val_td = (
                f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                f'border-radius:10px;font-size:12px;white-space:nowrap">{esc(r["val_short"])}</span>'
                f'<div style="color:#999;font-size:11px;margin-top:2px">{esc(r["val_detail"])}</div>'
            )

        if r["tier"] == "未知":
            act_td = f'<span style="color:{MUTED}">—</span>'
        else:
            act_td = f'<span style="color:{fg};font-weight:600">{esc(tier_action(r["tier"]))}</span>'

        h.append('<tr style="border-bottom:1px solid #f0f0f0">')
        h.append(f'<td style="padding:10px 8px;vertical-align:top">{fund_td}</td>')
        h.append(f'<td style="padding:10px 8px;vertical-align:top">{nav_td}</td>')
        h.append(f'<td style="padding:10px 8px;vertical-align:top;white-space:nowrap">{ret_td}</td>')
        h.append(f'<td style="padding:10px 8px;vertical-align:top;white-space:nowrap">{pnl_td}</td>')
        h.append(f'<td style="padding:10px 8px;vertical-align:top">{val_td}</td>')
        h.append(f'<td style="padding:10px 8px;vertical-align:top">{act_td}</td>')
        h.append("</tr>")
    h.append("</table>")

    # ---- 二、总仓位概览 ----
    h.append(_section("总仓位概览"))
    if pos_alert and pos_alert[0] == "incomplete":
        h.append(
            '<div style="color:#e65100;margin:4px 0">'
            "⚠️ 稳健类 / 黄金金额未配置，暂无法计算总仓位占比（请在 config 补全）</div>"
        )
    else:
        rows_p = [
            ("权益类", equity_total, BLUE),
            ("稳健类（纯债+理财）", stable, GREEN),
            ("黄金", gold, ORANGE),
        ]
        h.append(
            '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:4px" '
            'cellpadding="0" cellspacing="0">'
        )
        h.append('<tr style="background:#f5f7fa">')
        for th in ["类别", "金额(元)", "占比"]:
            h.append(
                f'<th style="padding:8px;text-align:left;color:#7a7f86;font-size:12px;'
                f'font-weight:600;border-bottom:2px solid #e5e8ec">{th}</th>'
            )
        h.append("</tr>")
        for label, amt, color in rows_p:
            share = amt / total_assets if total_assets > 0 else 0
            h.append('<tr style="border-bottom:1px solid #f0f0f0">')
            h.append(
                f'<td style="padding:8px"><span style="color:{color};font-weight:600">{label}</span></td>'
            )
            h.append(f'<td style="padding:8px">{money(amt)}</td>')
            h.append(
                f'<td style="padding:8px">'
                f'<span style="color:{color};font-weight:600">{share * 100:.1f}%</span></td>'
            )
            h.append("</tr>")
        h.append("</table>")
        if monthly_dca > 0:
            h.append(
                f'<div style="margin-top:6px;color:#616161;font-size:13px">'
                f"当前每月定投计划约 <b>{money(monthly_dca)} 元</b></div>"
            )

    if pos_alert and pos_alert[0] == "high":
        dca_half = monthly_dca / 2 if monthly_dca > 0 else None
        half_txt = f"（{money(monthly_dca)} → {money(dca_half)} 元）" if dca_half else ""
        h.append(
            f'<div style="margin-top:8px;padding:10px;background:#ffebee;border-left:4px solid {RED};'
            f'color:{RED};border-radius:4px">'
            f"⚠️ 权益类占比 {pct0(pos_alert[1])} &gt; 60%，建议<b>所有权益类基金定投金额统一减半</b>"
            f"{half_txt}（仓位过重，优先控仓）</div>"
        )
    elif pos_alert and pos_alert[0] == "low":
        h.append(
            f'<div style="margin-top:8px;padding:10px;background:#e8f5e9;border-left:4px solid {GREEN};'
            f'color:{GREEN};border-radius:4px">'
            f"📈 权益类占比 {pct0(pos_alert[1])} &lt; 25%，仓位偏低，建议从理财拨出资金加仓</div>"
        )

    # ---- 三、操作建议 ----
    h.append(_section("操作建议"))

    if buy_list:
        h.append(f'<div style="margin:6px 0;font-weight:600;color:{GREEN}">买入 / 加仓</div>')
        h.append('<ul style="margin:0 0 12px;padding-left:20px">')
        deep_low_months_threshold = strat.get("funding", {}).get("deep_low_months", 3)
        for name, tier, fund_text, deep_months in buy_list:
            s = f"<b>{esc(name)}</b> · {esc(fund_text)}"
            h.append(f'<li style="margin:3px 0;color:{GREEN}">{s}</li>')
            if tier == "深度低估" and deep_months >= deep_low_months_threshold:
                surplus_note = ""
                if monthly_surplus > 0:
                    surplus_note = f"月结余从 {money(monthly_surplus)} 翻倍至 {money(monthly_surplus * 2)}，"
                lump = strat.get("funding", {}).get("deep_low_lump_high", 30000) // 10000
                h.append(
                    f'<li style="margin:3px 0 6px;color:#1b5e20">'
                    f"　↳ 已连续 {deep_months} 个月深度低估：{surplus_note}再拨 {lump} 万追加买入</li>"
                )
        h.append("</ul>")

    if sell_list:
        h.append(f'<div style="margin:6px 0;font-weight:600;color:{RED}">暂停定投</div>')
        h.append('<ul style="margin:0 0 12px;padding-left:20px">')
        for name, tier, action in sell_list:
            color = ORANGE if tier == "偏高" else RED
            h.append(f'<li style="margin:3px 0;color:{color}"><b>{esc(name)}</b> · {esc(action)}</li>')
        h.append("</ul>")

    if not buy_list and not sell_list:
        h.append('<div style="margin:6px 0;color:#616161">今日无买卖信号，照计划定投即可。</div>')
        if hold_list:
            h.append('<ul style="margin:0;padding-left:20px">')
            for name, action in hold_list:
                h.append(f'<li style="margin:3px 0;color:#616161"><b>{esc(name)}</b> · {esc(action)}</li>')
            h.append("</ul>")
    elif hold_list:
        h.append('<div style="margin:6px 0;font-weight:600;color:#616161">其余产品（正常定投）</div>')
        h.append('<ul style="margin:0;padding-left:20px">')
        for name, action in hold_list:
            h.append(f'<li style="margin:3px 0;color:#616161"><b>{esc(name)}</b> · {esc(action)}</li>')
        h.append("</ul>")

    # ---- 四、汇率与换汇成本（QDII）----
    if fx:
        h.append(_section("汇率与换汇成本（QDII）"))
        pos = fx["pos"]
        latest = fx["latest"]
        if pos <= fx["extreme_pct"]:
            fx_note = (
                f"人民币处于近 3 年<b>升值极端</b>（USD/CNY {latest:.4f}，"
                f"处于近 3 年最低 {pos * 100:.1f}%）。此时<b>买入 QDII 换汇成本低、划算</b>；"
                "但已持仓 QDII 折算人民币的收益会被汇率侵蚀。"
            )
            fx_color = GREEN
        elif pos >= 1 - fx["extreme_pct"]:
            fx_note = (
                f"人民币处于近 3 年<b>贬值极端</b>（USD/CNY {latest:.4f}，"
                f"处于近 3 年最高 {pos * 100:.1f}%）。此时<b>买入 QDII 换汇成本高</b>；"
                "但已持仓 QDII 折算人民币收益会被汇率增厚。"
            )
            fx_color = RED
        else:
            fx_note = (
                f"当前 USD/CNY {latest:.4f}，处于近 3 年 {pos * 100:.1f}% 分位，"
                "汇率中性，无极端换汇成本提示。"
            )
            fx_color = GRAY
        h.append(
            f'<div style="margin:6px 0;padding:10px;border:1px solid #eee;'
            f'border-left:4px solid {fx_color};border-radius:4px;color:#333">{fx_note}</div>'
        )

    # ---- 五、估值档位变化 ----
    if change_list:
        h.append(_section("估值档位变化"))
        h.append('<ul style="margin:0;padding-left:20px">')
        for name, prev, tier in change_list:
            h.append(
                f'<li style="margin:3px 0;color:#e65100">⚠️ <b>{esc(name)}</b>：'
                f"{esc(prev)} → {esc(tier)}</li>"
            )
        h.append("</ul>")

    # ---- 六、单日异动 ----
    if dip_list:
        h.append(_section("单日异动提醒"))
        h.append('<ul style="margin:0;padding-left:20px">')
        for name, day_str in dip_list:
            h.append(f'<li style="margin:3px 0;color:#e65100">⚠️ <b>{esc(name)}</b> · 日涨跌 {esc(day_str)}</li>')
        h.append("</ul>")

    # ---- 七、数据异常 ----
    if errors:
        h.append(_section("数据异常"))
        h.append('<ul style="margin:0;padding-left:20px">')
        for e in errors:
            h.append(f'<li style="margin:3px 0;color:#c62828">❌ {esc(e)}</li>')
        h.append("</ul>")

    # ---- 八、每月校准提醒 ----
    calib_day = strat.get("calibration_day", 1)
    if dt.day == calib_day:
        h.append(_section("月度持仓校准"))
        h.append(
            '<div style="margin:6px 0;padding:10px;background:#fff8e1;'
            f'border-left:4px solid {ORANGE};border-radius:4px;color:#6d4c00">'
            "📅 今天是每月校准日。报告里的份额/成本是<b>按定投计划自动估算</b>的，"
            "可能与实际有偏差。请把四只产品的<b>最新持有份额 + 持仓成本</b>（或最新持有收益率）"
            "发给我，我来校准，保证数字准确。"
            "</div>"
        )

    # 底部说明
    h.append(
        '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #eee;'
        'color:#999;font-size:12px">'
        "红利：股息率&gt;4.5% 或 PB&lt;0.85 低估，股息率&lt;3.5% 或 PB&gt;1.2 高估 · "
        "宽基：PE 百分位 &lt;20% 深度低估 / 20~30% 低估 / &gt;70% 偏高 / &gt;80% 高估 · "
        "数据来自天天基金、蛋卷、frankfurter，仅供个人参考，不构成投资建议。"
        "</div>"
    )

    h.append("</div>")
    return "".join(h)


def _section(title):
    return (
        f'<div style="font-size:15px;font-weight:600;color:#2c3e50;'
        f'border-left:4px solid #2c3e50;padding-left:8px;margin:20px 0 8px">{esc(title)}</div>'
    )


# ---------------------------------------------------------------------------
# 邮件发送
# ---------------------------------------------------------------------------

def send_email(cfg, subject, html_body):
    smtp = cfg["email"]["smtp"]
    user = cfg["email"]["user"]
    to = cfg["email"]["to"]

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((Header("定投日报", "utf-8").encode(), user))
    msg["To"] = to

    server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30)
    try:
        server.login(user, os.environ.get("SMTP_PASSWORD", ""))
        server.sendmail(user, [to], msg.as_string())
    finally:
        server.quit()


def main():
    cfg = load_config()
    state = load_state()

    body = build_report(cfg, state)
    print(body)

    password = os.environ.get("SMTP_PASSWORD") or cfg["email"].get("password")
    if not password:
        print("\n[提示] 未设置 SMTP_PASSWORD，跳过发送邮件（也不写入 state.json）。")
        return

    save_state(state)

    dt = now_cn()
    subject = f"【定投日报】{dt.strftime('%Y-%m-%d')} {WEEKDAY_CN[dt.weekday()]}"
    try:
        send_email(cfg, subject, body)
        print(f"\n[ok] 邮件已发送至 {cfg['email']['to']}")
    except Exception as e:
        print(f"\n[error] 邮件发送失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
