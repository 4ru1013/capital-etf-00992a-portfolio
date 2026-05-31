import argparse
import datetime as dt
import pathlib
import time
from typing import Iterable

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from download_00992a import (
    ETF_CODE,
    PORTFOLIO_URL,
    compute_diff,
    ensure_dir,
    parse_stock_sheet,
    write_summary_markdown,
)


def parse_date(value: str) -> dt.date:
    value = value.strip().replace("/", "-")
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def iter_weekdays(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur += dt.timedelta(days=1)


def set_portfolio_date(page, target: dt.date) -> None:
    date_dash = target.strftime("%Y-%m-%d")
    date_slash = target.strftime("%Y/%m/%d")

    # Try native date input first.
    native = page.locator("input[type='date']")
    if native.count() > 0:
        native.first.fill(date_dash)
        native.first.dispatch_event("change")
        page.wait_for_timeout(1000)
        return

    # Try common visible text inputs.
    inputs = page.locator("input")
    count = inputs.count()
    for i in range(count):
        el = inputs.nth(i)
        try:
            box = el.bounding_box()
            if box is None:
                continue
            placeholder = (el.get_attribute("placeholder") or "").lower()
            value = el.input_value(timeout=1000)
            candidate = any(k in placeholder for k in ["日期", "date", "yyyy", "年", "/"]) or "/" in value or "-" in value
            if not candidate:
                continue
            el.click()
            el.fill(date_slash)
            el.press("Enter")
            page.wait_for_timeout(1200)
            return
        except Exception:
            continue

    # Last resort: set every input that looks date-like through JS and dispatch events.
    changed = page.evaluate(
        """
        ([dateDash, dateSlash]) => {
          let changed = 0;
          for (const el of document.querySelectorAll('input')) {
            const ph = (el.placeholder || '').toLowerCase();
            const val = el.value || '';
            if (el.type === 'date') {
              el.value = dateDash;
            } else if (ph.includes('date') || ph.includes('yyyy') || ph.includes('日期') || val.includes('/') || val.includes('-')) {
              el.value = dateSlash;
            } else {
              continue;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            changed += 1;
          }
          return changed;
        }
        """,
        [date_dash, date_slash],
    )
    if not changed:
        raise RuntimeError("找不到可設定日期的輸入欄位。")
    page.wait_for_timeout(1200)


def download_excel_for_date(page, raw_dir: pathlib.Path, target: dt.date) -> pathlib.Path:
    data_date = target.strftime("%Y%m%d")
    out_path = raw_dir / f"{ETF_CODE}_portfolio_{data_date}.xlsx"
    if out_path.exists():
        print(f"[SKIP] Raw Excel exists: {out_path}")
        return out_path

    set_portfolio_date(page, target)

    try:
        page.locator("button", has_text="下載資料").first.wait_for(timeout=30000)
    except PlaywrightTimeoutError as exc:
        debug_path = raw_dir / f"{ETF_CODE}_debug_{data_date}.html"
        debug_path.write_text(page.content(), encoding="utf-8")
        raise RuntimeError(f"找不到下載資料按鈕，已保存 debug HTML: {debug_path}") from exc

    with page.expect_download(timeout=90000) as download_info:
        page.locator("button", has_text="下載資料").first.click()
    download = download_info.value
    download.save_as(str(out_path))
    print(f"[OK] Downloaded {data_date}: {out_path}")
    return out_path


def save_outputs(excel_path: pathlib.Path, out_dir: pathlib.Path, data_date: str) -> bool:
    holdings_path = out_dir / f"{ETF_CODE}_holdings_{data_date}.csv"
    if holdings_path.exists():
        print(f"[SKIP] Holdings exists: {holdings_path}")
        return False

    holdings_df = parse_stock_sheet(excel_path)
    holdings_df.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Saved holdings: {holdings_path}")
    return True


def rebuild_diffs(out_dir: pathlib.Path) -> None:
    files = sorted(out_dir.glob(f"{ETF_CODE}_holdings_*.csv"))
    if len(files) < 2:
        return
    for prev_path, curr_path in zip(files[:-1], files[1:]):
        data_date = curr_path.stem.split("_")[-1]
        diff_path = out_dir / f"{ETF_CODE}_diff_{data_date}.csv"
        md_path = out_dir / f"{ETF_CODE}_diff_{data_date}.md"
        if diff_path.exists() and md_path.exists():
            continue
        prev_df = pd.read_csv(prev_path, dtype={"code": "string"})
        curr_df = pd.read_csv(curr_path, dtype={"code": "string"})
        diff_df = compute_diff(prev_df, curr_df)
        diff_df.to_csv(diff_path, index=False, encoding="utf-8-sig")
        write_summary_markdown(diff_df, md_path, data_date)
        print(f"[OK] Rebuilt diff: {diff_path}")


def update_latest(out_dir: pathlib.Path) -> None:
    files = sorted(out_dir.glob(f"{ETF_CODE}_holdings_*.csv"))
    if not files:
        return
    latest = files[-1]
    latest_df = pd.read_csv(latest, dtype={"code": "string"})
    latest_df.to_csv(out_dir / f"{ETF_CODE}_latest.csv", index=False, encoding="utf-8-sig")
    print(f"[OK] Updated latest from {latest.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--max-days", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    if end < start:
        raise ValueError("end must be >= start")

    dates = list(iter_weekdays(start, end))
    if len(dates) > args.max_days:
        raise RuntimeError(f"日期數 {len(dates)} 超過 max-days={args.max_days}，請分批執行。")

    base = pathlib.Path("data")
    raw_dir = base / "raw"
    out_dir = base / "out"
    ensure_dir(raw_dir)
    ensure_dir(out_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale="zh-TW")
        page = context.new_page()
        print(f"[INFO] Open page: {PORTFOLIO_URL}")
        page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=90000)

        for target in dates:
            data_date = target.strftime("%Y%m%d")
            try:
                excel_path = download_excel_for_date(page, raw_dir, target)
                save_outputs(excel_path, out_dir, data_date)
            except Exception as exc:
                print(f"[WARN] {data_date} failed: {exc}")
            time.sleep(args.sleep)

        browser.close()

    rebuild_diffs(out_dir)
    update_latest(out_dir)


if __name__ == "__main__":
    main()
