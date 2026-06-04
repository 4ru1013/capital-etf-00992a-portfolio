import datetime as dt
import pathlib
import re
from typing import Optional

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ETF_CODE = "00992A"
ETF_NAME = "群益台灣科技創新主動式ETF"
PORTFOLIO_URL = "https://www.capitalfund.com.tw/etf/product/detail/500/portfolio"


def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def normalize_text(s) -> str:
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


def extract_date_from_filename_or_text(value: str) -> Optional[str]:
    for y, m, d in re.findall(r"(20\d{2})[/-]?(\d{1,2})[/-]?(\d{1,2})", value):
        try:
            date_obj = dt.date(int(y), int(m), int(d))
        except ValueError:
            continue
        if dt.date(2020, 1, 1) <= date_obj <= dt.date.today() + dt.timedelta(days=7):
            return date_obj.strftime("%Y%m%d")
    return None


def download_official_excel(raw_dir: pathlib.Path) -> pathlib.Path:
    ensure_dir(raw_dir)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale="zh-TW")
        page = context.new_page()
        print(f"[INFO] Open page: {PORTFOLIO_URL}")
        page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=90000)

        try:
            page.locator("button", has_text="下載資料").first.wait_for(timeout=30000)
        except PlaywrightTimeoutError as exc:
            html_path = raw_dir / f"{ETF_CODE}_debug_page.html"
            html_path.write_text(page.content(), encoding="utf-8")
            raise RuntimeError(f"找不到下載資料按鈕，已保存 debug HTML: {html_path}") from exc

        with page.expect_download(timeout=90000) as download_info:
            page.locator("button", has_text="下載資料").first.click()

        download = download_info.value
        suggested = download.suggested_filename or f"{ETF_CODE}.xlsx"
        if not suggested.lower().endswith((".xlsx", ".xls")):
            suggested = f"{ETF_CODE}.xlsx"

        data_date = extract_date_from_filename_or_text(suggested) or dt.date.today().strftime("%Y%m%d")
        out_path = raw_dir / f"{ETF_CODE}_portfolio_{data_date}.xlsx"
        download.save_as(str(out_path))
        print(f"[OK] Downloaded official Excel: {out_path}")
        browser.close()
        return out_path


def locate_header_row(df: pd.DataFrame) -> Optional[int]:
    for idx in range(len(df)):
        values = [normalize_text(x) for x in df.iloc[idx].tolist()]
        joined = "|".join(values)
        if "股票代號" in joined and "股票名稱" in joined and "持股權重" in joined and "股數" in joined:
            return idx
    return None


def parse_stock_sheet(excel_path: pathlib.Path) -> pd.DataFrame:
    xls = pd.ExcelFile(excel_path)
    print(f"[INFO] Excel sheets: {xls.sheet_names}")

    candidates = []
    for sheet in xls.sheet_names:
        raw = pd.read_excel(excel_path, sheet_name=sheet, header=None, dtype=object)
        header_idx = locate_header_row(raw)
        if header_idx is not None:
            candidates.append((sheet, raw, header_idx))

    if not candidates:
        raise RuntimeError("Excel 中找不到含有『股票代號／股票名稱／持股權重／股數』的股票分頁。")

    sheet, raw, header_idx = sorted(candidates, key=lambda x: ("股票" not in str(x[0]), str(x[0])))[0]
    print(f"[INFO] Selected stock sheet: {sheet}, header row: {header_idx}")

    headers = [normalize_text(x) for x in raw.iloc[header_idx].tolist()]
    body = raw.iloc[header_idx + 1 :].copy()
    body.columns = headers

    def find_col(keyword: str) -> str:
        for c in body.columns:
            if keyword in str(c):
                return c
        raise RuntimeError(f"找不到欄位：{keyword}，目前欄位={list(body.columns)}")

    code_col = find_col("股票代號")
    name_col = find_col("股票名稱")
    weight_col = find_col("持股權重")
    shares_col = find_col("股數")

    df = body[[code_col, name_col, weight_col, shares_col]].copy()
    df.columns = ["code", "name", "weight", "shares"]
    df["code"] = df["code"].map(normalize_text)
    df["name"] = df["name"].map(normalize_text)
    df["weight"] = df["weight"].map(to_float_safe)
    df["shares"] = df["shares"].map(to_int_safe)

    df = df[df["code"].str.fullmatch(r"\d{4}[A-Za-z]?", na=False)].copy()
    df = df[df["shares"] > 0].copy()
    df = df.groupby(["code", "name"], as_index=False).agg({"shares": "sum", "weight": "sum"})
    df = df.sort_values(["weight", "shares"], ascending=[False, False]).reset_index(drop=True)

    if len(df) <= 10:
        raise RuntimeError(f"解析後只有 {len(df)} 檔，疑似仍非完整 Excel 股票分頁。")

    print(f"[OK] Parsed {len(df)} stock holdings from Excel.")
    return df[["code", "name", "shares", "weight"]]


def compute_diff(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    prev = prev_df.copy()
    curr = curr_df.copy()
    prev["code"] = prev["code"].astype("string").str.strip()
    curr["code"] = curr["code"].astype("string").str.strip()

    if "weight" not in prev.columns:
        prev["weight"] = 0.0
    if "weight" not in curr.columns:
        curr["weight"] = 0.0

    prev = prev.rename(columns={"shares": "prev_shares", "weight": "prev_weight"})
    curr = curr.rename(columns={"shares": "curr_shares", "weight": "curr_weight"})

    merged = prev.merge(curr, on=["code"], how="outer", suffixes=("_prev", "_curr"))
    merged["name"] = merged.get("name_curr", "").fillna("")
    if "name_prev" in merged.columns:
        merged.loc[merged["name"].eq("") | merged["name"].isna(), "name"] = merged["name_prev"].fillna("")

    for col in ["prev_shares", "curr_shares"]:
        merged[col] = merged[col].fillna(0).astype(int)
    for col in ["prev_weight", "curr_weight"]:
        merged[col] = merged[col].fillna(0.0).astype(float)

    merged["shares_delta"] = merged["curr_shares"] - merged["prev_shares"]
    merged["weight_delta"] = merged["curr_weight"] - merged["prev_weight"]

    def status_row(r):
        if r["prev_shares"] == 0 and r["curr_shares"] > 0:
            return "NEW"
        if r["prev_shares"] > 0 and r["curr_shares"] == 0:
            return "OUT"
        if r["shares_delta"] > 0:
            return "UP"
        if r["shares_delta"] < 0:
            return "DOWN"
        if abs(r["weight_delta"]) >= 0.01:
            return "WEIGHT_CHANGE"
        return "SAME"

    merged["status"] = merged.apply(status_row, axis=1)
    order_map = {"NEW": 0, "UP": 1, "DOWN": 2, "OUT": 3, "WEIGHT_CHANGE": 4, "SAME": 5}
    merged["order"] = merged["status"].map(order_map).fillna(99)
    merged = merged.sort_values(["order", "weight_delta", "shares_delta"], ascending=[True, False, False]).drop(columns=["order"])

    return merged[[
        "code", "name", "prev_shares", "curr_shares", "shares_delta",
        "prev_weight", "curr_weight", "weight_delta", "status"
    ]].reset_index(drop=True)


def write_summary_markdown(diff_df: pd.DataFrame, out_md: pathlib.Path, data_date: str) -> None:
    def top_rows(status, n=20):
        sub = diff_df[diff_df["status"] == status].copy()
        if status in ("DOWN", "OUT"):
            sub = sub.sort_values(["weight_delta", "shares_delta"], ascending=[True, True])
        else:
            sub = sub.sort_values(["weight_delta", "shares_delta"], ascending=[False, False])
        return sub.head(n)

    lines = [f"# {ETF_CODE} Holdings Diff ({data_date})\n\n"]
    counts = diff_df["status"].value_counts().to_dict()
    lines.append("## Summary\n\n")
    lines.append(
        f"- NEW: {counts.get('NEW',0)} | UP: {counts.get('UP',0)} | DOWN: {counts.get('DOWN',0)} | "
        f"OUT: {counts.get('OUT',0)} | WEIGHT_CHANGE: {counts.get('WEIGHT_CHANGE',0)} | SAME: {counts.get('SAME',0)}\n\n"
    )

    for status, label in [("NEW", "新增持股"), ("UP", "加碼"), ("DOWN", "減碼"), ("OUT", "出清"), ("WEIGHT_CHANGE", "權重變化")]:
        sub = top_rows(status)
        lines.append(f"## {label} ({status})\n\n")
        if sub.empty:
            lines.append("_None_\n\n")
            continue
        lines.append("| code | name | prev shares | curr shares | shares delta | prev weight | curr weight | weight delta | status |\n")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|\n")
        for _, r in sub.iterrows():
            name = str(r["name"]).replace("|", " ")
            lines.append(
                f"| {r['code']} | {name} | {r['prev_shares']} | {r['curr_shares']} | {r['shares_delta']} | "
                f"{r['prev_weight']:.2f}% | {r['curr_weight']:.2f}% | {r['weight_delta']:.2f}% | {r['status']} |\n"
            )
        lines.append("\n")

    out_md.write_text("".join(lines), encoding="utf-8")


def main():
    base = pathlib.Path("data")
    raw_dir = base / "raw"
    out_dir = base / "out"
    holdings_dir = out_dir / "holdings"
    diff_csv_dir = out_dir / "diff" / "csv"
    diff_md_dir = out_dir / "diff" / "md"

    for folder in [raw_dir, holdings_dir, diff_csv_dir, diff_md_dir]:
        ensure_dir(folder)

    excel_path = download_official_excel(raw_dir)
    data_date = extract_date_from_filename_or_text(excel_path.name) or dt.date.today().strftime("%Y%m%d")
    holdings_df = parse_stock_sheet(excel_path)

    holdings_path = holdings_dir / f"{ETF_CODE}_holdings_{data_date}.csv"
    holdings_df.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Saved standardized holdings to {holdings_path}")

    latest_path = holdings_dir / f"{ETF_CODE}_latest.csv"
    root_latest_path = out_dir / f"{ETF_CODE}_latest.csv"
    old_latest_path = out_dir / f"{ETF_CODE}_latest.csv"

    if latest_path.exists():
        prev_df = pd.read_csv(latest_path, dtype={"code": "string"})
    elif old_latest_path.exists():
        prev_df = pd.read_csv(old_latest_path, dtype={"code": "string"})
    else:
        prev_df = None

    if prev_df is not None:
        if {"code", "shares"}.issubset(set(prev_df.columns)):
            diff_df = compute_diff(prev_df, holdings_df)
            diff_csv_path = diff_csv_dir / f"{ETF_CODE}_diff_{data_date}.csv"
            diff_md_path = diff_md_dir / f"{ETF_CODE}_diff_{data_date}.md"
            diff_df.to_csv(diff_csv_path, index=False, encoding="utf-8-sig")
            write_summary_markdown(diff_df, diff_md_path, data_date)
            print(f"[OK] Saved diff CSV to {diff_csv_path}")
            print(f"[OK] Saved diff MD to {diff_md_path}")
        else:
            print("[WARN] latest.csv format invalid; diff skipped.")
    else:
        print("[INFO] No previous latest.csv found; diff skipped.")

    holdings_df.to_csv(latest_path, index=False, encoding="utf-8-sig")
    holdings_df.to_csv(root_latest_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Updated latest to {latest_path}")
    print(f"[OK] Updated root latest to {root_latest_path}")


if __name__ == "__main__":
    main()
