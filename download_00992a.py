import datetime as dt
import pathlib
import re
from typing import Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

ETF_CODE = "00992A"
ETF_NAME = "群益台灣科技創新主動式ETF"
PORTFOLIO_URL = "https://www.capitalfund.com.tw/etf/product/detail/500/portfolio"


# -----------------------------
# Utilities
# -----------------------------
def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def to_int_safe(x) -> int:
    if x is None:
        return 0
    s = str(x).strip().replace(",", "").replace(" ", "")
    if s == "" or s.lower() == "nan":
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def to_float_safe(x) -> float:
    if x is None:
        return 0.0
    s = str(x).strip().replace("%", "").replace(",", "")
    if s == "" or s.lower() == "nan":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def extract_data_date(text: str) -> Optional[str]:
    """
    Extract YYYYMMDD from the official page.
    The page usually shows dates like 2026/05/29 near NAV / portfolio section.
    Pick the latest date found, bounded loosely by today + 2 days.
    """
    candidates = []
    for y, m, d in re.findall(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text):
        try:
            date_obj = dt.date(int(y), int(m), int(d))
        except ValueError:
            continue
        if date_obj <= dt.date.today() + dt.timedelta(days=2):
            candidates.append(date_obj)

    if not candidates:
        return None
    return max(candidates).strftime("%Y%m%d")


# -----------------------------
# Core: Download, Parse, Diff
# -----------------------------
def download_html(session: requests.Session) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.capitalfund.com.tw/etf/product",
    }
    resp = session.get(PORTFOLIO_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    print(f"[INFO] 下載官方頁面成功：{resp.url}")
    print(f"[INFO] Content-Type: {resp.headers.get('Content-Type')}")
    return resp.text


def parse_holdings_from_html(html: str) -> Tuple[pd.DataFrame, Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    data_date = extract_data_date(text)
    if data_date:
        print(f"[INFO] data_date = {data_date}")
    else:
        print("[WARN] 無法從頁面抓到資料日期，將以今天日期做檔名。")

    # Official page text pattern around holdings:
    # 股票代號 \n 股票名稱 ... \n 持股權重(%) \n 股數 \n 2330 \n 台積電 \n 8.07% \n 1,761,000
    # This regex intentionally requires shares, so duplicated display-only rows without shares are ignored.
    pattern = re.compile(
        r"(?m)^\s*(\d{4}[A-Za-z]?)\s*$\s*\n"
        r"^\s*([^\n\d%][^\n]*)\s*$\s*\n"
        r"^\s*(\d+(?:\.\d+)?)\s*%\s*$\s*\n"
        r"^\s*([\d,]+)\s*$"
    )

    rows = []
    for code, name, weight, shares in pattern.findall(text):
        code = normalize_text(code)
        name = normalize_text(name)
        rows.append(
            {
                "code": code,
                "name": name,
                "weight": to_float_safe(weight),
                "shares": to_int_safe(shares),
            }
        )

    if not rows:
        # Fallback: parse all visible lines by state machine.
        lines = [normalize_text(x) for x in text.splitlines() if normalize_text(x)]
        for i, line in enumerate(lines):
            if not re.fullmatch(r"\d{4}[A-Za-z]?", line):
                continue
            window = lines[i : i + 8]
            if len(window) < 4:
                continue
            name = window[1]
            weight = None
            shares = None
            for item in window[2:]:
                if weight is None and re.fullmatch(r"\d+(?:\.\d+)?%", item):
                    weight = item
                elif weight is not None and shares is None and re.fullmatch(r"[\d,]+", item):
                    shares = item
                    break
            if weight and shares and not re.search(r"股票|代號|名稱|權重|股數", name):
                rows.append(
                    {
                        "code": line,
                        "name": name,
                        "weight": to_float_safe(weight),
                        "shares": to_int_safe(shares),
                    }
                )

    if not rows:
        preview = "\n".join(text.splitlines()[:120])
        raise RuntimeError(
            "找不到 00992A 持股資料。可能是群益網站 HTML 結構改版或資料改由 JS API 載入。\n\n"
            f"頁面文字預覽：\n{preview}"
        )

    df = pd.DataFrame(rows)
    df["code"] = df["code"].astype("string").str.strip()
    df["name"] = df["name"].astype("string").str.strip()
    df["shares"] = df["shares"].astype(int)
    df["weight"] = df["weight"].astype(float)

    # Remove duplicated rows caused by responsive duplicate tables.
    # If the same code appears more than once, keep the row with the largest shares; weight is usually identical.
    df = (
        df.sort_values(["code", "shares"], ascending=[True, False])
        .drop_duplicates(subset=["code"], keep="first")
        .sort_values(["weight", "shares"], ascending=[False, False])
        .reset_index(drop=True)
    )

    if len(df) < 5:
        raise RuntimeError(f"解析到的持股數過少：{len(df)}，請檢查官方頁面格式是否改版。")

    return df, data_date


def compute_diff(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    """
    Return diff dataframe with columns:
      code, name, prev_shares, curr_shares, delta, status
    status: NEW / OUT / UP / DOWN / SAME
    """
    prev = prev_df.copy()
    curr = curr_df.copy()

    prev["code"] = prev["code"].astype("string").str.strip()
    curr["code"] = curr["code"].astype("string").str.strip()

    prev = prev.rename(columns={"shares": "prev_shares"})
    curr = curr.rename(columns={"shares": "curr_shares"})

    merged = prev.merge(curr, on=["code"], how="outer", suffixes=("_prev", "_curr"))

    merged["name"] = merged.get("name_curr", "").fillna("")
    if "name_prev" in merged.columns:
        merged.loc[merged["name"].eq("") | merged["name"].isna(), "name"] = merged["name_prev"].fillna("")

    merged["prev_shares"] = merged["prev_shares"].fillna(0).astype(int)
    merged["curr_shares"] = merged["curr_shares"].fillna(0).astype(int)
    merged["delta"] = merged["curr_shares"] - merged["prev_shares"]

    def status_row(r):
        if r["prev_shares"] == 0 and r["curr_shares"] > 0:
            return "NEW"
        if r["prev_shares"] > 0 and r["curr_shares"] == 0:
            return "OUT"
        if r["delta"] > 0:
            return "UP"
        if r["delta"] < 0:
            return "DOWN"
        return "SAME"

    merged["status"] = merged.apply(status_row, axis=1)

    order_map = {"NEW": 0, "UP": 1, "DOWN": 2, "OUT": 3, "SAME": 4}
    merged["order"] = merged["status"].map(order_map).fillna(99)
    merged = merged.sort_values(["order", "delta"], ascending=[True, False]).drop(columns=["order"])

    return merged[["code", "name", "prev_shares", "curr_shares", "delta", "status"]].reset_index(drop=True)


def write_summary_markdown(diff_df: pd.DataFrame, out_md: pathlib.Path, data_date: str) -> None:
    def top_rows(status, n=20):
        sub = diff_df[diff_df["status"] == status].copy()
        if status in ("DOWN", "OUT"):
            sub = sub.sort_values("delta")
        elif status in ("UP", "NEW"):
            sub = sub.sort_values("delta", ascending=False)
        return sub.head(n)

    lines = []
    lines.append(f"# {ETF_CODE} Holdings Diff ({data_date})\n\n")

    counts = diff_df["status"].value_counts().to_dict()
    lines.append("## Summary\n\n")
    lines.append(
        f"- NEW: {counts.get('NEW',0)} | UP: {counts.get('UP',0)} | "
        f"DOWN: {counts.get('DOWN',0)} | OUT: {counts.get('OUT',0)} | SAME: {counts.get('SAME',0)}\n\n"
    )

    for sec, label in [("NEW", "新增持股"), ("UP", "加碼"), ("DOWN", "減碼"), ("OUT", "出清")]:
        sub = top_rows(sec, n=20)
        lines.append(f"## {label} ({sec})\n\n")
        if sub.empty:
            lines.append("_None_\n\n")
            continue
        lines.append("| code | name | prev | curr | delta | status |\n")
        lines.append("|---|---|---:|---:|---:|---|\n")
        for _, r in sub.iterrows():
            lines.append(
                f"| {r['code']} | {str(r['name']).replace('|',' ')} | "
                f"{r['prev_shares']} | {r['curr_shares']} | {r['delta']} | {r['status']} |\n"
            )
        lines.append("\n")

    out_md.write_text("".join(lines), encoding="utf-8")


def main():
    base = pathlib.Path("data")
    raw_dir = base / "raw"
    out_dir = base / "out"
    ensure_dir(raw_dir)
    ensure_dir(out_dir)

    session = requests.Session()
    html = download_html(session)

    holdings_df, data_date = parse_holdings_from_html(html)
    if not data_date:
        data_date = dt.date.today().strftime("%Y%m%d")

    raw_path = raw_dir / f"{ETF_CODE}_portfolio_{data_date}.html"
    if raw_path.exists():
        print(f"[INFO] Raw HTML already exists: {raw_path}")
    else:
        raw_path.write_text(html, encoding="utf-8")
        print(f"[OK] Saved HTML snapshot to {raw_path}")

    holdings_path = out_dir / f"{ETF_CODE}_holdings_{data_date}.csv"
    holdings_df.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Saved standardized holdings to {holdings_path}")

    latest_path = out_dir / f"{ETF_CODE}_latest.csv"

    if latest_path.exists():
        prev_df = pd.read_csv(latest_path, dtype={"code": "string"})
        if "code" in prev_df.columns:
            prev_df["code"] = prev_df["code"].str.strip()

        if not {"code", "shares"}.issubset(set(prev_df.columns)):
            print("[WARN] latest.csv 格式不對，將略過 diff。")
        else:
            diff_df = compute_diff(prev_df, holdings_df)
            diff_path = out_dir / f"{ETF_CODE}_diff_{data_date}.csv"
            diff_df.to_csv(diff_path, index=False, encoding="utf-8-sig")
            print(f"[OK] Saved diff to {diff_path}")

            md_path = out_dir / f"{ETF_CODE}_diff_{data_date}.md"
            write_summary_markdown(diff_df, md_path, data_date)
            print(f"[OK] Saved diff summary to {md_path}")
    else:
        print("[INFO] No previous latest.csv found; diff skipped (first run).")

    holdings_df.to_csv(latest_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Updated latest to {latest_path}")


if __name__ == "__main__":
    main()
