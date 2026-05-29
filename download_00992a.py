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

    # Prefer sheet whose name contains 股票; otherwise use the first matched sheet.
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
    df = df.sort_values(["weight", "shares"], ascending=[False, False]).reset_index(drop=True)

    if len(df) <= 10:
        raise RuntimeError(f"解析後只有 {len(df)} 檔，疑似仍非完整 Excel 股票分頁。")

    print(f"[OK] Parsed {len(df)} stock holdings from Excel.")
    return df


def compute_diff(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
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
    lines = [f"# {ETF_CODE} Holdings Diff ({data_date})\n\n"]
    counts = diff_df["status"].value_counts().to_dict()
    lines.append("## Summary\n\n")
    lines.append(
        f"- NEW: {counts.get('NEW',0)} | UP: {counts.get('UP',0)} | "
        f"DOWN: {counts.get('DOWN',0)} | OUT: {counts.get('OUT',0)} | SAME: {counts.get('SAME',0)}\n\n"
    )
    for status, label in [("NEW", "新增持股"), ("UP", "加碼"), ("DOWN", "減碼"), ("OUT", "出清")]:
        sub = diff_df[diff_df["status"] == status].copy()
        if status in ("DOWN", "OUT"):
            sub = sub.sort_values("delta")
        else:
            sub = sub.sort_values("delta", ascending=False)
        sub = sub.head(20)
        lines.append(f"## {label} ({status})\n\n")
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

    excel_path = download_official_excel(raw_dir)
    data_date = extract_date_from_filename_or_text(excel_path.name) or dt.date.today().strftime("%Y%m%d")
    holdings_df = parse_stock_sheet(excel_path)

    holdings_path = out_dir / f"{ETF_CODE}_holdings_{data_date}.csv"
    holdings_df.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Saved standardized holdings to {holdings_path}")

    latest_path = out_dir / f"{ETF_CODE}_latest.csv"
    if latest_path.exists():
        prev_df = pd.read_csv(latest_path, dtype={"code": "string"})
        if {"code", "shares"}.issubset(set(prev_df.columns)):
            diff_df = compute_diff(prev_df, holdings_df)
            diff_path = out_dir / f"{ETF_CODE}_diff_{data_date}.csv"
            diff_df.to_csv(diff_path, index=False, encoding="utf-8-sig")
            print(f"[OK] Saved diff to {diff_path}")
            md_path = out_dir / f"{ETF_CODE}_diff_{data_date}.md"
            write_summary_markdown(diff_df, md_path, data_date)
            print(f"[OK] Saved diff summary to {md_path}")
        else:
            print("[WARN] latest.csv format invalid; diff skipped.")
    else:
        print("[INFO] No previous latest.csv found; diff skipped.")

    holdings_df.to_csv(latest_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Updated latest to {latest_path}")


if __name__ == "__main__":
    main()
