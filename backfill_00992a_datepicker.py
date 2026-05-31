import argparse
import datetime as dt
import pathlib
import re
import time

import pandas as pd
from playwright.sync_api import sync_playwright

from download_00992a import ETF_CODE, PORTFOLIO_URL, compute_diff, ensure_dir, parse_stock_sheet, write_summary_markdown

MONTH_ZH = {1:'一月',2:'二月',3:'三月',4:'四月',5:'五月',6:'六月',7:'七月',8:'八月',9:'九月',10:'十月',11:'十一月',12:'十二月'}


def parse_date(s):
    return dt.datetime.strptime(s.strip().replace('/', '-'), '%Y-%m-%d').date()


def iter_dates(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def get_calendar_month(page):
    txt = page.locator('body').inner_text(timeout=5000)
    year_match = re.search(r'(20\d{2})', txt)
    if not year_match:
        return None
    year = int(year_match.group(1))
    for m, zh in MONTH_ZH.items():
        if zh in txt:
            return year, m
    month_match = re.search(r'(\d{1,2})\s*月', txt)
    if month_match:
        return year, int(month_match.group(1))
    return None


def click_date_input(page):
    inputs = page.locator('input')
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            val = el.input_value(timeout=1000)
            if re.search(r'20\d{2}[/-]\d{1,2}[/-]\d{1,2}', val) and el.bounding_box():
                el.click()
                page.wait_for_timeout(700)
                return
        except Exception:
            pass
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            if el.bounding_box():
                el.click()
                page.wait_for_timeout(700)
                return
        except Exception:
            pass
    raise RuntimeError('date input not found')


def click_month_arrow(page, direction):
    # direction: -1 previous month, +1 next month.
    buttons = page.locator('button')
    candidates = []
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            box = b.bounding_box()
            if box:
                candidates.append((box['x'], box['y'], b))
        except Exception:
            pass
    if not candidates:
        # fallback by screen coordinate near calendar header
        if direction < 0:
            page.mouse.click(60, 305)
        else:
            page.mouse.click(335, 305)
        page.wait_for_timeout(600)
        return
    candidates.sort(key=lambda x: (x[1], x[0]))
    if direction < 0:
        candidates[0][2].click()
    else:
        candidates[-1][2].click()
    page.wait_for_timeout(600)


def goto_month(page, target):
    for _ in range(36):
        ym = get_calendar_month(page)
        if ym == (target.year, target.month):
            return
        if ym is None:
            raise RuntimeError('calendar month not detected')
        cur = ym[0] * 12 + ym[1]
        tgt = target.year * 12 + target.month
        click_month_arrow(page, -1 if cur > tgt else 1)
    raise RuntimeError('cannot navigate to target month')


def click_day(page, target):
    day = str(target.day)
    loc = page.locator(f'text="{day}"')
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.inner_text(timeout=1000).strip() == day and el.bounding_box():
                el.click()
                page.wait_for_timeout(1800)
                return
        except Exception:
            pass
    raise RuntimeError(f'day not found: {day}')


def select_date(page, target):
    click_date_input(page)
    goto_month(page, target)
    click_day(page, target)
    page.wait_for_timeout(1500)


def download_for_date(page, raw_dir, target):
    ymd = target.strftime('%Y%m%d')
    out = raw_dir / f'{ETF_CODE}_portfolio_{ymd}.xlsx'
    select_date(page, target)
    with page.expect_download(timeout=90000) as info:
        page.locator('button', has_text='下載資料').first.click()
    info.value.save_as(str(out))
    print(f'[OK] downloaded {ymd}: {out}')
    return out


def save_holdings(excel_path, out_dir, target):
    ymd = target.strftime('%Y%m%d')
    out = out_dir / f'{ETF_CODE}_holdings_{ymd}.csv'
    df = parse_stock_sheet(excel_path)
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'[OK] saved holdings {out}')


def rebuild_diffs(out_dir):
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


def update_latest(out_dir):
    files = sorted(out_dir.glob(f'{ETF_CODE}_holdings_*.csv'))
    if not files:
        return
    latest = pd.read_csv(files[-1], dtype={'code': 'string'})
    latest.to_csv(out_dir / f'{ETF_CODE}_latest.csv', index=False, encoding='utf-8-sig')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--max-days', type=int, default=5)
    args = ap.parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end)
    dates = list(iter_dates(start, end))
    if len(dates) > args.max_days:
        raise RuntimeError('too many dates; split into smaller batches')
    raw_dir = pathlib.Path('data/raw')
    out_dir = pathlib.Path('data/out')
    ensure_dir(raw_dir)
    ensure_dir(out_dir)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale='zh-TW', viewport={'width': 390, 'height': 1200})
        page = context.new_page()
        page.goto(PORTFOLIO_URL, wait_until='networkidle', timeout=90000)
        for d in dates:
            try:
                xlsx = download_for_date(page, raw_dir, d)
                save_holdings(xlsx, out_dir, d)
            except Exception as exc:
                print(f'[WARN] failed {d:%Y%m%d}: {exc}')
            time.sleep(1)
        browser.close()
    rebuild_diffs(out_dir)
    update_latest(out_dir)


if __name__ == '__main__':
    main()
