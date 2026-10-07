#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from quantcheck.picks_check import ROOT, STATE, SHOTS, PROFILE, BASE, NY, load_env, ensure_login
from quantcheck.state import atomic_write_json

LATEST = STATE / 'site_snapshot_latest.json'
PREVIOUS = STATE / 'site_snapshot_previous.json'


def collect_page(page, url: str, name: str):
    # `networkidle` is too brittle on Market Tools: the page embeds market/news
    # widgets that can keep polling or hang. Wait for the document and stable
    # visible content instead, then give widgets a short hydration window.
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_load_state('load', timeout=15000)
    page.wait_for_function(
        """() => {
          const main = document.querySelector('main');
          const text = (main ? main.innerText : document.body.innerText || '').trim();
          return text.length > 40;
        }""",
        timeout=15000,
    )
    page.wait_for_timeout(3500)
    js = r'''
    () => {
      const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const texts = sel => [...document.querySelectorAll(sel)].filter(visible).map(e => e.innerText || e.textContent || '').map(s => s.trim().replace(/\s+/g,' ')).filter(Boolean);
      const links = [...document.querySelectorAll('a[href]')].filter(visible).map(a => ({text:(a.innerText||a.textContent||'').trim().replace(/\s+/g,' '), href:a.href})).filter(x => x.text || x.href);
      const buttons = [...document.querySelectorAll('button')].filter(visible).map(b => (b.innerText||b.textContent||'').trim().replace(/\s+/g,' ')).filter(Boolean);
      const headings = texts('h1,h2,h3');
      const nav = texts('nav a, aside a, [role="navigation"] a');
      const main = document.querySelector('main') ? document.querySelector('main').innerText.trim().replace(/\s+/g,' ') : document.body.innerText.trim().replace(/\s+/g,' ');
      return {title: document.title, url: location.href, headings, nav, buttons, links, main_text_sample: main.slice(0, 5000)};
    }
    '''
    data = page.evaluate(js)
    data['name'] = name
    return data


def main():
    env = load_env()
    if LATEST.exists():
        PREVIOUS.write_text(LATEST.read_text(encoding='utf-8'), encoding='utf-8')
    ts = datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=True, viewport={'width': 1440, 'height': 1100})
        page = ctx.new_page()
        ensure_login(page, env)
        pages = [
            ('dashboard', f'{BASE}/quantgt-picks'),
            ('monthly', f'{BASE}/quantgt-picks'),
            ('weekly', f'{BASE}/weekly-picks'),
            ('tradingview_indicator', f'{BASE}/tradingview-indicator'),
            ('ai_winners', f'{BASE}/who-is-winning-ai'),
            ('rrg', f'{BASE}/rrg'),
            ('market_tools', f'{BASE}/market-tools'),
            ('study_guide', f'{BASE}/learn'),
        ]
        collected = []
        screenshots = {}
        # If a page times out, keep the previous good page in latest snapshot and
        # record a capture_warning. A timeout is monitor uncertainty, not proof
        # that the site removed headings/nav/buttons.
        previous_by_name = {}
        if LATEST.exists():
            try:
                old_snapshot = json.loads(LATEST.read_text(encoding='utf-8'))
                previous_by_name = {p.get('name'): p for p in old_snapshot.get('pages', []) if p.get('name')}
            except Exception:
                previous_by_name = {}
        for name, url in pages:
            try:
                item = collect_page(page, url, name)
                collected.append(item)
                if name == 'dashboard':
                    shot = SHOTS / f'site_dashboard_{ts}.png'
                    page.screenshot(path=str(shot), full_page=True)
                    screenshots[name] = str(shot)
            except Exception as e:
                fallback = dict(previous_by_name.get(name) or {'name': name, 'url': url})
                fallback['capture_warning'] = str(e)
                fallback['name'] = name
                fallback['url'] = url
                collected.append(fallback)
        ctx.close()
    snapshot = {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'pages': collected,
        'screenshots': screenshots,
    }
    raw_dir = STATE / 'raw_site'
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"site_snapshot_raw_{datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')}.json"
    atomic_write_json(raw_path, snapshot)
    atomic_write_json(LATEST, snapshot)
    print(json.dumps({'status': 'ok', 'pages': len(collected), 'latest': str(LATEST), 'raw': str(raw_path), 'previous_exists': PREVIOUS.exists()}, ensure_ascii=False))

if __name__ == '__main__':
    main()
