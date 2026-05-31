import argparse
import pathlib

from playwright.sync_api import sync_playwright

from download_00992a import PORTFOLIO_URL, ensure_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=False)
    parser.add_argument('--end', required=False)
    parser.add_argument('--max-days', type=int, default=5)
    parser.parse_args()

    ensure_dir(pathlib.Path('data/out'))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, locale='zh-TW', viewport={'width': 390, 'height': 1200})
        page = context.new_page()
        page.goto(PORTFOLIO_URL, wait_until='networkidle', timeout=90000)
        page.wait_for_timeout(3000)

        print('========== PAGE TITLE ==========', flush=True)
        print(page.title(), flush=True)

        print('========== VISIBLE ELEMENTS ==========', flush=True)
        elements = page.evaluate(
            """
            () => {
              const out = [];
              const selectors = 'input, button, a, select, [role=button], [class*=date], [class*=Date], [class*=calendar], [class*=Calendar], [class*=picker], [class*=Picker]';
              for (const el of document.querySelectorAll(selectors)) {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                if (!visible) continue;
                out.push({
                  tag: el.tagName,
                  type: el.getAttribute('type') || '',
                  role: el.getAttribute('role') || '',
                  cls: el.className || '',
                  id: el.id || '',
                  text: (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 120),
                  x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
                });
              }
              return out;
            }
            """
        )
        for i, e in enumerate(elements):
            print(f"#{i} tag={e['tag']} type={e['type']} role={e['role']} x={e['x']} y={e['y']} w={e['w']} h={e['h']} id={e['id']} class={e['cls']} text={e['text']}", flush=True)

        print('========== BODY FIRST 12000 ==========', flush=True)
        body = page.locator('body').inner_text(timeout=10000)
        print(body[:12000], flush=True)
        print('========== DEBUG STOP ==========', flush=True)
        browser.close()

    raise RuntimeError('debug stop after visible element dump')


if __name__ == '__main__':
    main()
