import argparse
import datetime as dt
import pathlib
import time

import pandas as pd
from playwright.sync_api import sync_playwright

from download_00992a import (
    ETF_CODE,
    PORTFOLIO_URL,
    compute_diff,
    ensure_dir,
    parse_stock_sheet,
    write_summary_markdown,
)


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s.strip().replace('/', '-'), '%Y-%m-%d').date()


def iter_dates(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def set_condition_date(page, target: dt.date) -> None:
    value = target.strftime('%Y/%m/%d')
    field = page.locator('#condition-date')
    field.wait_for(state='visible', timeout=30000)
    field.scroll_into_view_if_needed(timeout=10000)
    field.click()
    field.fill(value)
    page.evaluate(
        """
        (value) => {
          const el = document.querySelector('#condition-date');
          if (!el) throw new Error('condition-date input not found');
          el.value = value;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          el.dispatchEvent(new Event('blur', { bubbles: true }));
        }
        """,
        value,
    )
    page.keyboard.press('Enter')
    page.wait_for_timeout(1200)
    current = field.input_value(timeout=3000)
    print(f'[INFO] condition-date set to {current}')
    if current != value:
        raise RuntimeError(f'condition-date value mismatch: expected {value}, got {current}')


def download_one(page, raw_dir: pathlib.Path, out_dir: pathlib.Path, target: dt.date) -> pathlib.Path:
    ymd = target.strftime('%Y%m%d')
    xlsx_path = raw_dir / f'{ETF_CODE}_portfolio_{ymd}.xlsx'
    csv_path = out_dir / f'{ETF_CODE}_holdings_{ymd}.csv'

    set_condition_date(page, target)
    with page.expect_download(timeout=90000) as info:
        page.locator('button', has_text='下載資料').first.click()
    info.value.save_as(str(xlsx_path))
    print(f'[OK] downloaded {ymd}: {xlsx_path}')

    df = parse_stock_sheet(xlsx_path)
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f'[OK] saved holdings {csv_path}, rows={len(df)}')
    if len(df) == 0:
        raise RuntimeError(f'empty holdings after parsing {ymd}')
    return csv_path


def rebuild_diffs(out_dir: pathlib.Path) -> None:
    files = sorted(out_dir.glob(f'{ETF_CODE}_holdings_*.csv'))
    for prev_path, curr_path in zip(files[:-1], files[1:]):
        ymd = curr_path.stem.split('_')[-1]
        diff_path = out_dir / f'{ETF_CODE}_diff_{ymd}.csv'
        md_path = out_dir / f'{ETF_CODE}_diff_{ymd}.md'
        prev = pd.read_csv(prev_path, dtype={'code': 'string'})
        curr = pd.read_csv(curr_path, dtype={'code': 'string'})
        diff = compute_diff(prev, curr)
        diff.to_csv(diff_path, index=False, encoding='utf-8-sig')
        write_summary_markdown(diff, md_path, ymd)
        print(f'[OK] rebuilt diff {diff_path}')


def update_latest(out_dir: pathlib.Path) -> None:
    files = sorted(out_dir.glob(f'{ETF_CODE}_holdings_*.csv'))
    if not files:
        return
    latest = pd.read_csv(files[-1], dtype={'code': 'string'})
    latest.to_csv(out_dir / f'{ETF_CODE}_latest.csv', index=False, encoding='utf-8-sig')
    print(f'[OK] updated latest from {files[-1].name}')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--max-days', type=int, default=5)
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)
    dates = list(iter_dates(start, end))
    if len(dates) > args.max_days:
        raise RuntimeError('too many dates; split into smaller batches')

    raw_dir = pathlib.Path('data/raw')
    out_dir = pathlib.Path('data/out')
    ensure_dir(raw_dir)
    ensure_dir(out_dir)

    saved = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale='zh-TW', viewport={'width': 390, 'height': 1200})
        page = context.new_page()
        page.goto(PORTFOLIO_URL, wait_until='networkidle', timeout=90000)
        for target in dates:
            saved.append(download_one(page, raw_dir, out_dir, target))
            time.sleep(1)
        browser.close()

    if not saved:
        raise RuntimeError('no holdings files saved')
    rebuild_diffs(out_dir)
    update_latest(out_dir)


if __name__ == '__main__':
    main()
