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


def get_calendar_text(page):
    for sel in ['ngb-datepicker', '.ngb-dp', '.dropdown-menu', '.bs-datepicker', '.mat-datepicker-content']:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                txt = loc.first.inner_text(timeout=1500)
                if txt.strip():
                    return txt
            except Exception:
                pass
    return ''


def get_calendar_month(page):
    txt = get_calendar_text(page)
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
    label = page.locator('text=最新日期').last
    if label.count() == 0:
        raise RuntimeError('latest-date label not found')
    label.scroll_into_view_if_needed(timeout=5000)
    page.wait_for_timeout(500)
    box = label.bounding_box()
    if not box:
        raise RuntimeError('latest-date label has no bounding box')
    # The actual date field is visually below or near the label on mobile.
    click_points = [
        (box['x'] + box['width'] / 2, box['y'] + box['height'] + 42),
        (box['x'] + box['width'] / 2, box['y'] + box['height'] + 70),
        (195, min(box['y'] + box['height'] + 42, 900)),
        (195, min(box['y'] + box['height'] + 70, 930)),
    ]
    for x, y in click_points:
        print(f'[INFO] try click latest-date field at x={x:.1f}, y={y:.1f}')
        page.mouse.click(x, y)
        page.wait_for_timeout(900)
        if get_calendar_text(page).strip():
            print('[INFO] calendar opened')
            return
    raise RuntimeError('calendar did not open after clicking latest-date field')


def click_month_arrow(page, direction):
    for sel in ['ngb-datepicker button', '.ngb-dp button', '.dropdown-menu button', '.bs-datepicker button', '.mat-datepicker-content button']:
        buttons = page.locator(sel)
        visible = []
        for i in range(buttons.count()):
            b = buttons.nth(i)
            try:
                box = b.bounding_box()
                if box:
                    visible.append((box['x'], box['y'], b))
            except Exception:
                pass
        if len(visible) >= 2:
            visible.sort(key=lambda x: (x[1], x[0]))
            if direction < 0:
                visible[0][2].click()
            else:
                visible[-1][2].click()
            page.wait_for_timeout(800)
            return
    raise RuntimeError('calendar arrow buttons not found')


def goto_month(page, target):
    for _ in range(36):
        ym = get_calendar_month(page)
        print(f'[INFO] calendar month detected: {ym}, target: {(target.year, target.month)}')
        if ym == (target.year, target.month):
            return
        if ym is None:
            print('[DEBUG] calendar text:')
            print(get_calendar_text(page))
            raise RuntimeError('calendar month not detected')
        cur = ym[0] * 12 + ym[1]
        tgt = target.year * 12 + target.month
        click_month_arrow(page, -1 if cur > tgt else 1)
    raise RuntimeError('cannot navigate to target month')


def click_day(page, target):
    day = str(target.day)
    for sel in ['ngb-datepicker', '.ngb-dp', '.dropdown-menu', '.bs-datepicker', '.mat-datepicker-content']:
        container = page.locator(sel)
        if container.count() == 0:
            continue
        loc = container.locator(f'text="{day}"')
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.inner_text(timeout=1000).strip() == day and el.bounding_box():
                    print(f'[INFO] click date {target:%Y-%m-%d} by text {day}')
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
    print(f'[OK] saved holdings {out}, rows={len(df)}')
    return out


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
    saved = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale='zh-TW', viewport={'width': 390, 'height': 1200})
        page = context.new_page()
        page.goto(PORTFOLIO_URL, wait_until='networkidle', timeout=90000)
        for d in dates:
            xlsx = download_for_date(page, raw_dir, d)
            saved.append(save_holdings(xlsx, out_dir, d))
            time.sleep(1)
        browser.close()
    if not saved:
        raise RuntimeError('no holdings files saved')
    rebuild_diffs(out_dir)
    update_latest(out_dir)


if __name__ == '__main__':
    main()
