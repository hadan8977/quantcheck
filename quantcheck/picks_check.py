#!/usr/bin/env python3
"""
Quant GT pick monitor.

Modes:
  --mode baseline  Fetch current data and initialize state without notification.
  --mode check     Trading-window aware check; notify only on data change/failure.
  --mode fetch     Fetch current data and print summary.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Reuse the existing fetch/export implementation so Excel formatting stays in one place.
from quantcheck.config import load_env as load_dotenv
from quantcheck.diff import compare
from quantcheck import picks_email
from quantcheck import picks_report as report
from quantcheck.notify_dedupe import should_send_notification
from quantcheck.gmail_api_notify import send_email_per_recipient as deliver_email
from quantcheck.email_templates import build_card_email_html
from quantcheck.notify_routes import EmailRoute, recipients_for_route, route_label
from quantcheck.state import atomic_write_json, prune_old_files as prune_files
from quantcheck.validation import validate_member_picks_data

ROOT = Path(os.environ.get('QUANTCHECK_HOME', Path(__file__).resolve().parents[1]))
STATE = ROOT / 'state'
OUTPUT = ROOT / 'output'
SHOTS = ROOT / 'screenshots'
LOGS = ROOT / 'logs'
LATEST = STATE / 'latest_picks.json'
PREVIOUS = STATE / 'previous_picks.json'
HEALTH = STATE / 'health.json'
LAST_CHANGE_NOTIFICATION = STATE / 'last_picks_change_notification.json'
LOG_FILE = LOGS / 'quantgt_monitor.log'
BASE = 'https://quantgt.io'
NY = ZoneInfo('America/New_York')
WINDOWS = {
    'premarket_0830': (8, 30),
    'premarket_0900': (9, 0),
    'daily_non_trading_1200': (12, 0),
    'postmarket_1700': (17, 0),
}

for d in [STATE, OUTPUT, SHOTS, LOGS]:
    d.mkdir(parents=True, exist_ok=True)


def load_env() -> Dict[str, str]:
    return load_dotenv(ROOT)


def log(msg: str, echo: bool = False):
    ts = datetime.now(timezone.utc).isoformat()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(f'[{ts}] {msg}\n')
    if echo:
        print(msg)


def json_dump(path: Path, obj: Any):
    atomic_write_json(path, obj)


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def trading_day(dt_ny: datetime) -> bool:
    cal = mcal.get_calendar('NYSE')
    start = dt_ny.date().isoformat()
    sched = cal.schedule(start_date=start, end_date=start)
    return not sched.empty


def current_window(dt_ny: datetime, tolerance_minutes: int = 20) -> str | None:
    for name, (h, m) in WINDOWS.items():
        target = dt_ny.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((dt_ny - target).total_seconds()) <= tolerance_minutes * 60:
            return name
    return None


def strip_dynamic(data: Dict[str, Any]) -> Dict[str, Any]:
    d = copy.deepcopy(data)
    d.pop('fetched_at', None)
    return d


def section_title(default: str, section: Dict[str, Any]) -> str:
    if section.get('kind') == 'watchlist':
        return 'Weekly Watchlist'
    return default


def build_notification_html(
    data: Dict[str, Any],
    diff: Dict[str, Any] | None = None,
    context: str = 'change',
    previous: Dict[str, Any] | None = None,
    banner: str | None = None,
) -> str:
    """Subscriber picks email HTML. `context` is internal run metadata and is
    deliberately not rendered; pass `banner` for a visible note (admin test,
    delayed resend). Layout lives in quantcheck.picks_email."""
    return picks_email.build_html(data, diff, previous=previous, banner=banner)


def build_system_alert_html(title: str, cards: List[Dict[str, Any]], context: str = 'system alert') -> str:
    return build_card_email_html(title, cards, context=context)


def build_notification_body(
    data: Dict[str, Any],
    diff: Dict[str, Any] | None = None,
    context: str = 'change',
    previous: Dict[str, Any] | None = None,
    banner: str | None = None,
) -> str:
    return picks_email.build_text(data, diff, previous=previous, banner=banner)


def build_telegram_body(data: Dict[str, Any], diff: Dict[str, Any] | None = None, context: str = 'change') -> str:
    monthly = data.get('monthly', {})
    weekly = data.get('weekly', {})
    fetched = data.get('fetched_at') or now_utc()
    lines = [
        'Quant GT Monitor',
        f'Context: {context}',
        f'Fetched: {fetched}',
        '',
        'Summary:',
        f"- Portfolio: {monthly.get('pick_date', 'Unknown')} · {len(monthly.get('rows', []) or [])} stocks",
        f"- {section_title('Weekly Picks', weekly)}: {weekly.get('pick_date', 'Unknown')} · {len(weekly.get('rows', []) or [])} stocks",
    ]
    if diff is not None:
        summary = summarize_diff(diff, compact=True)
        lines += ['', 'Changes:', summary]
    else:
        lines += ['', 'Changes: not evaluated in this manual test']
    lines += ['', 'See attached Excel and screenshots for details.']
    return '\n'.join(lines)


def summarize_diff(diff: Dict[str, Any], compact: bool = False) -> str:
    lines = []
    for section, title in [('monthly', 'Portfolio'), ('weekly', 'Weekly Watchlist')]:
        d = diff.get(section, {})
        if not d.get('changed_flag'):
            continue
        if compact:
            bits = []
            if d.get('date'):
                bits.append('date')
            if d.get('added'):
                bits.append(f"+{len(d['added'])}")
            if d.get('removed'):
                bits.append(f"-{len(d['removed'])}")
            if d.get('changed'):
                bits.append(f"{len(d['changed'])} rows changed")
            lines.append(f"- {title}: " + (', '.join(bits) if bits else 'changed'))
            continue
        lines.append(f'{title} changed')
        if d.get('date'):
            lines.append(f"- Date: {d['date']['old']} -> {d['date']['new']}")
        if d.get('added'):
            lines.append('- Added: ' + ', '.join(d['added']))
        if d.get('removed'):
            lines.append('- Removed: ' + ', '.join(d['removed']))
        for item in d.get('changed', [])[:12]:
            field_bits = []
            for field, vals in list(item['fields'].items())[:5]:
                field_bits.append(f"{field}: {vals['old']} -> {vals['new']}")
            lines.append(f"- {item['symbol']}: " + '; '.join(field_bits))
        if len(d.get('changed', [])) > 12:
            lines.append(f"- ... {len(d['changed']) - 12} more changed rows")
    return '\n'.join(lines) if lines else 'No changes.'


def send_telegram(text: str, media: List[Path] | None = None):
    msg = text
    for p in media or []:
        if p and p.exists():
            msg += f"\nMEDIA:{p}"
    return


def send_email(
    subject: str,
    body: str,
    attachments: List[Path] | None = None,
    html_body: str | None = None,
    route: EmailRoute = EmailRoute.PICKS_UPDATE,
):
    env = load_env()
    recipients = recipients_for_route(route, env)
    if not recipients:
        log(f'email skipped: {route_label(route)} not configured for {subject}')
        return
    delivered, failed = deliver_email(subject, body, to=recipients, attachments=attachments or [], html=html_body)
    if failed:
        log(f'email retrying via {route.value} for {len(failed)} recipient(s): {", ".join(failed)}: {subject}')
        retry_delivered, retry_failed = deliver_email(subject, body, to=failed, attachments=attachments or [], html=html_body)
        delivered = [*delivered, *[r for r in retry_delivered if r not in delivered]]
        failed = retry_failed
    if delivered:
        log(f'email sent via {route.value} to {", ".join(delivered)}: {subject}')
    if failed:
        log(f'email FAILED via {route.value} after retry for {len(failed)} recipient(s): {", ".join(failed)}: {subject}')
    if failed:
        raise RuntimeError(f'email delivery failed after retry for {len(failed)} recipient(s): {", ".join(failed)}: {subject}')
    if not delivered:
        raise RuntimeError(f'email send failed or no sender configured: {subject}')
    return delivered, failed


def notify(
    subject: str,
    body: str,
    media: List[Path] | None = None,
    html_body: str | None = None,
    telegram_body: str | None = None,
    route: EmailRoute = EmailRoute.PICKS_UPDATE,
):
    send_email(subject, body, media, html_body=html_body, route=route)
    tg = telegram_body or body
    note = {'subject': subject, 'body': tg, 'email_body': body, 'media': [str(p) for p in media or [] if p.exists()], 'at': now_utc()}
    json_dump(STATE / 'last_notification.json', note)
    print('\n'.join([subject, tg] + [f'ATTACHMENT:{p}' for p in media or [] if p.exists()]))


def ensure_login(page, env: Dict[str, str]):
    page.goto(f'{BASE}/quantgt-picks', wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_load_state('load', timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)
    if report.has_auth_session(page) and report.has_picks_content(page) and not report.is_login_prompt_visible(page):
        return
    email = env.get('QUANTGT_EMAIL')
    password = env.get('QUANTGT_PASSWORD')
    if not email or not password:
        raise RuntimeError('Missing QUANTGT_EMAIL/QUANTGT_PASSWORD in .env')
    page.goto(f'{BASE}/login?redirect=/quantgt-picks', wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_load_state('load', timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)
    if page.get_by_role('button', name=re.compile('log in', re.I)).count() > 0:
        page.get_by_role('button', name=re.compile('log in', re.I)).click()
    email_box = page.get_by_placeholder('you@example.com') if page.get_by_placeholder('you@example.com').count() else page.locator('input[type="email"]')
    pass_box = page.get_by_placeholder('min. 8 characters') if page.get_by_placeholder('min. 8 characters').count() else page.locator('input[type="password"]')
    email_box.fill(email)
    pass_box.fill(password)
    page.get_by_role('button', name=re.compile('log in|sign in', re.I)).click()
    try:
        page.wait_for_load_state('domcontentloaded', timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    page.goto(f'{BASE}/quantgt-picks', wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_load_state('load', timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)
    report.wait_for_picks_content(page)
    report.assert_authenticated_page(page, 'monthly')


def _wait_for_screenshot_ready(page, name: str) -> None:
    """Wait for the page to finish hydrating before taking an email screenshot.

    Playwright's full_page screenshot uses the document height at capture time. On
    Quant GT weekly picks the shell/table can appear first at ~1200px tall, then
    the full Top 10 list and expanded detail row hydrate a second or two later at
    ~1800px. Capturing immediately produced truncated weekly attachments.
    """
    report.wait_for_picks_content(page)
    rows = report.wait_for_parsable_picks_rows(page, name)
    if name == 'weekly' and len(rows) < 10:
        raise RuntimeError(f'weekly screenshot not ready: expected 10 parsed rows, got {len(rows)}')
    try:
        page.wait_for_function(
            r"""(mode) => {
              const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              if (mode === 'weekly') {
                const text = (document.querySelector('main')?.innerText || document.body.innerText || '').replace(/\s+/g, ' ');
                if (/\bWatchlist\b/i.test(text) || /\/weekly-picks\b/i.test(location.pathname)) {
                  return height >= window.innerHeight;
                }
                return height > window.innerHeight + 400;
              }
              return height >= window.innerHeight;
            }""",
            arg=name,
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)


def dismiss_screenshot_overlays(page) -> None:
    """Close optional product tours so email screenshots show unobscured data."""
    dialog = page.get_by_role('dialog', name=re.compile(r'Latest Holdings', re.I))
    if dialog.count() == 0:
        return
    close = dialog.get_by_role('button', name=re.compile(r'close', re.I))
    if close.count() == 0:
        close = dialog.locator('button[aria-label*="close" i], button:has-text("×")')
    if close.count() > 0:
        close.click(timeout=3000)
        page.wait_for_timeout(300)


def _assert_screenshot_symbols_match(page, name: str, expected_rows: List[Dict[str, Any]]) -> None:
    expected_symbols = [str(row.get('symbol') or '').strip().upper() for row in expected_rows if row.get('symbol')]
    page_rows = report.rows_from_table(page, name)
    page_symbols = [str(row.get('symbol') or '').strip().upper() for row in page_rows if row.get('symbol')]
    if name == 'weekly':
        expected_symbols = expected_symbols[:10]
        page_symbols = page_symbols[:10]
    if expected_symbols != page_symbols:
        raise RuntimeError(f'{name} screenshot symbols mismatch: expected={expected_symbols} actual={page_symbols}')


def capture_logged_in_screenshots(which: List[str], expected_data: Dict[str, Any] | None = None) -> Dict[str, Path]:
    env = load_env()
    ts = datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, locale='en-US', storage_state=None)
        page = context.new_page()
        ensure_login(page, env)
        targets = {
            'monthly': f'{BASE}/quantgt-picks',
            'weekly': f'{BASE}/weekly-picks',
        }
        for name in which:
            page.goto(targets[name], wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('load', timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
            _wait_for_screenshot_ready(page, name)
            dismiss_screenshot_overlays(page)
            if report.is_login_prompt_visible(page):
                raise RuntimeError(f'{name} page is not authenticated: login prompt visible')
            if not report.has_picks_content(page):
                raise RuntimeError(f'{name} page has no picks content')
            expected_rows = ((expected_data or {}).get(name, {}) or {}).get('rows') or []
            if expected_rows:
                _assert_screenshot_symbols_match(page, name, expected_rows)
            attachment_name = 'portfolio' if name == 'monthly' else 'watchlist'
            path = SHOTS / f'{attachment_name}_{ts}.png'
            page.screenshot(path=str(path), full_page=True)
            out[name] = path
        context.close()
        browser.close()
    return out


def prune_old_files(directory: Path, pattern: str, keep: int = 200):
    prune_files(directory, pattern, keep)


def fetch_current(max_attempts: int = 3, retry_delay_seconds: float = 8.0) -> Dict[str, Any]:
    env = load_env()
    os.environ.setdefault('QUANTGT_EMAIL', env.get('QUANTGT_EMAIL', ''))
    os.environ.setdefault('QUANTGT_PASSWORD', env.get('QUANTGT_PASSWORD', ''))
    report.EMAIL = os.environ.get('QUANTGT_EMAIL', '')
    report.PASSWORD = os.environ.get('QUANTGT_PASSWORD', '')

    errors: List[str] = []
    max_attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        try:
            data = report.fetch()
            validate_member_picks_data(data)
            if str(data.get('monthly', {}).get('pick_date') or '') == 'Unknown':
                log('warning: monthly pick date parsed as Unknown; date-only diff will be ignored')
            data['auth_verified'] = True
            data['source_policy'] = 'logged-in member page only; unauthenticated/demo data rejected'
            if attempt > 1:
                data['fetch_recovered_after_attempts'] = attempt
                log(f'fetch recovered on attempt {attempt}/{max_attempts}')
            return data
        except Exception as e:
            err = f'attempt {attempt}/{max_attempts}: {type(e).__name__}: {e}'
            errors.append(err)
            log('fetch attempt failed: ' + err)
            debug_dir = STATE / 'fetch_failures'
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / f"fetch_failure_{datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')}_attempt{attempt}.json"
            try:
                json_dump(debug_path, {'at': now_utc(), 'attempt': attempt, 'max_attempts': max_attempts, 'error': str(e), 'traceback': traceback.format_exc()[-4000:]})
                prune_old_files(debug_dir, 'fetch_failure_*.json', keep=80)
            except Exception:
                pass
            if attempt < max_attempts and retry_delay_seconds:
                time.sleep(retry_delay_seconds)
    raise RuntimeError('Quant GT fetch failed after retries: ' + ' | '.join(errors))


def write_health(**kwargs):
    old = {}
    if HEALTH.exists():
        try:
            old = json_load(HEALTH)
        except Exception:
            old = {}
    obj = {**old, **kwargs, 'updated_at': now_utc()}
    json_dump(HEALTH, obj)


def run_test_email(recipient: str | None = None):
    try:
        data = fetch_current()
        raw_dir = STATE / 'raw'
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"picks_raw_test_{datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')}.json"
        json_dump(raw_path, data)
        excel = report.export_excel(data)
        shots = capture_logged_in_screenshots(['monthly', 'weekly'], expected_data=data)
        tg_body = build_telegram_body(data, None, context='manual full-flow test')
        summary_body = build_notification_body(data, None)
        body = '\n'.join([
            'Quant GT Monitor test completed successfully.',
            'Route: administrators only',
            f"Monthly date: {data.get('monthly', {}).get('pick_date', 'Unknown')}",
            f"Weekly date: {data.get('weekly', {}).get('pick_date', 'Unknown')}",
            '',
            'Current Picks Summary:',
            summary_body,
            '',
            f"Excel: {excel}",
            f"Raw JSON: {raw_path}",
            'Screenshots:',
            *[f'{name}: {path}' for name, path in shots.items()],
        ])
        html_body = build_notification_html(data, None, banner='Admin test: full scrape, Excel and screenshot flow passed. Subscribers did not receive this.')
        media = [excel] + list(shots.values())
        route = EmailRoute.PICKS_UPDATE if recipient else EmailRoute.ADMIN
        to = [recipient] if recipient else None
        if recipient:
            delivered, failed = deliver_email(
                'Quant GT Monitor Test Passed',
                body,
                to=recipient,
                attachments=media,
                html=html_body,
            )
            if failed:
                raise RuntimeError(f'email delivery failed for {failed}')
            log(f'email sent via explicit recipient to {recipient}: Quant GT Monitor Test Passed')
        else:
            notify(
                'Quant GT Monitor Test Passed',
                body,
                media,
                html_body=html_body,
                telegram_body=tg_body,
                route=route,
            )
        write_health(
            last_run_at=now_utc(),
            last_success_at=now_utc(),
            last_error=None,
            consecutive_failures=0,
            last_window='manual_test_email',
            monthly_date=data['monthly']['pick_date'],
            weekly_date=data['weekly']['pick_date'],
        )
        print(json.dumps({'status': 'test_notification_sent', 'excel': str(excel), 'raw': str(raw_path), 'screenshots': {k: str(v) for k, v in shots.items()}, 'recipient': recipient}, ensure_ascii=False, indent=2))
    except Exception as e:
        tb = traceback.format_exc()
        log('manual test-email failed: ' + tb)
        health = json_load(HEALTH) if HEALTH.exists() else {}
        failures = int(health.get('consecutive_failures') or 0) + 1
        write_health(last_run_at=now_utc(), last_error=str(e), consecutive_failures=failures, last_window='manual_test_email')
        subject = 'Quant GT Monitor Test Failed'
        body = (
            'Manual full-flow test failed before notification could be sent.\n'
            'Route: administrators only\n'
            f'Error: {e}\n'
            f'Consecutive failures: {failures}\n\n'
            f'{tb[-3000:]}'
        )
        failure_shot = None
        try:
            shots = capture_logged_in_screenshots(['monthly'])
            failure_shot = shots.get('monthly')
        except Exception:
            pass
        failure_html = build_system_alert_html(
            subject,
            [
                {'label': 'Route', 'value': 'administrators only'},
                {'label': 'Status', 'value': 'Test failed before notification could be sent', 'tone': 'error'},
                {'label': 'Error', 'value': str(e), 'tone': 'error'},
                {'label': 'Consecutive Failures', 'value': failures, 'tone': 'warning'},
                {'label': 'Traceback', 'value': tb[-3000:], 'tone': 'error'},
            ],
            context='manual full-flow test failed',
        )
        notify(subject, body, [failure_shot] if failure_shot else [], html_body=failure_html, route=EmailRoute.ADMIN)
        raise


def run_baseline(echo: bool = True):
    data = fetch_current()
    if LATEST.exists():
        PREVIOUS.write_text(LATEST.read_text(encoding='utf-8'), encoding='utf-8')
    json_dump(LATEST, data)
    json_dump(ROOT / 'latest_picks.json', data)
    write_health(last_run_at=now_utc(), last_success_at=now_utc(), last_error=None, consecutive_failures=0,
                 monthly_date=data['monthly']['pick_date'], weekly_date=data['weekly']['pick_date'], mode='baseline')
    log('baseline initialized', echo=echo)
    if echo:
        print(json.dumps({'status': 'baseline_initialized', 'monthly': data['monthly']['pick_date'], 'weekly': data['weekly']['pick_date']}, ensure_ascii=False, indent=2))


def run_check(force=False, no_random=False):
    env = load_env()
    dt_ny = datetime.now(NY)
    window = current_window(dt_ny)
    if not force:
        is_trading = trading_day(dt_ny)
        if is_trading and window == 'daily_non_trading_1200':
            log(f'skip: non-trading daily window on trading day at {dt_ny.isoformat()}')
            return
        if not is_trading and window != 'daily_non_trading_1200':
            log(f'skip: not NYSE trading day and outside daily non-trading window at {dt_ny.isoformat()}')
            return
        if not window:
            log(f'skip: outside target window at {dt_ny.isoformat()}')
            return
    if not no_random:
        delay = random.randint(0, 300)
        log(f'random delay {delay}s')
        time.sleep(delay)
    try:
        data = fetch_current()
        raw_dir = STATE / 'raw'
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"picks_raw_{datetime.now(NY).strftime('%Y-%m-%d_%H%M%S')}.json"
        json_dump(raw_path, data)
        prune_old_files(raw_dir, 'picks_raw_*.json', keep=240)
        old = json_load(LATEST) if LATEST.exists() else None
        if old is None:
            json_dump(LATEST, data)
            json_dump(ROOT / 'latest_picks.json', data)
            write_health(last_run_at=now_utc(), last_success_at=now_utc(), last_error=None, consecutive_failures=0, last_window=window or 'forced', mode='check')
            log('initialized latest state, no notification')
            return
        diff = compare(strip_dynamic(old), strip_dynamic(data))
        write_health(last_run_at=now_utc(), last_success_at=now_utc(), last_error=None, consecutive_failures=0,
                     last_window=window or 'forced', monthly_date=data['monthly']['pick_date'], weekly_date=data['weekly']['pick_date'], changed=diff['changed'],
                     mode='check')
        if not diff['changed']:
            log('no pick changes')
            return
        if not should_send_notification(diff, data, dedupe_path=LAST_CHANGE_NOTIFICATION):
            write_health(last_run_at=now_utc(), last_success_at=now_utc(), last_error=None, consecutive_failures=0,
                         last_window=window or 'forced', monthly_date=data['monthly']['pick_date'], weekly_date=data['weekly']['pick_date'], changed=False,
                         duplicate_notification_suppressed=True)
            return
        PREVIOUS.write_text(LATEST.read_text(encoding='utf-8'), encoding='utf-8')
        json_dump(LATEST, data)
        json_dump(ROOT / 'latest_picks.json', data)
        excel = report.export_excel(data, diff=diff, previous=old)
        prune_old_files(OUTPUT, 'quantgt_picks_report_*.xlsx', keep=80)
        shots = capture_logged_in_screenshots(['monthly', 'weekly'], expected_data=data)
        prune_old_files(SHOTS, 'portfolio_*.png', keep=80)
        prune_old_files(SHOTS, 'watchlist_*.png', keep=80)
        prune_old_files(SHOTS, '*_picks_*.png', keep=160)
        tg_body = build_telegram_body(data, diff, context=f'picks changed · window={window or "forced"}')
        body = build_notification_body(data, diff, previous=old)
        html_body = build_notification_html(data, diff, previous=old)
        media = [excel] + list(shots.values())
        subject = picks_email.build_subject(diff, data)
        log(f'notifying subscribers: {subject} (window={window or "forced"})')
        notify(subject, body, media, html_body=html_body, telegram_body=tg_body)
    except Exception as e:
        tb = traceback.format_exc()
        log('check failed: ' + tb)
        health = json_load(HEALTH) if HEALTH.exists() else {}
        failures = int(health.get('consecutive_failures') or 0) + 1
        failure_shot = None
        try:
            shots = capture_logged_in_screenshots(['monthly', 'weekly'])
            failure_shot = shots.get('weekly')
        except Exception:
            pass
        write_health(last_run_at=now_utc(), last_error=str(e), consecutive_failures=failures, last_window=window or 'forced')
        subject = 'Quant GT Monitor Failed' if failures < 3 else f'Quant GT Monitor Consecutive Failures ({failures})'
        body = f'Window: {window or "forced"}\nError: {e}\nConsecutive failures: {failures}\n\n{tb[-2000:]}'
        failure_html = build_system_alert_html(
            subject,
            [
                {'label': 'Window', 'value': window or 'forced'},
                {'label': 'Error', 'value': str(e), 'tone': 'error'},
                {'label': 'Consecutive Failures', 'value': failures, 'tone': 'warning'},
                {'label': 'Traceback', 'value': tb[-3000:], 'tone': 'error'},
            ],
            context='scheduled monitor failed',
        )
        notify(subject, body, [failure_shot] if failure_shot else [], html_body=failure_html, route=EmailRoute.ADMIN)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['baseline', 'check', 'fetch', 'screenshot'], default='check')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--no-random', action='store_true')
    ap.add_argument('--quiet', action='store_true', help='Suppress baseline status output')
    ap.add_argument('--test-email', action='store_true', help='Fetch current picks, export Excel, capture screenshots, and send a test email notification')
    ap.add_argument('--recipient', help='Send the test email to one explicit recipient instead of the default route')
    args = ap.parse_args()

    if args.test_email:
        run_test_email(args.recipient)
        return
    if args.mode == 'baseline':
        run_baseline(echo=not args.quiet)
    elif args.mode == 'fetch':
        data = fetch_current()
        print(json.dumps({'monthly': data['monthly']['pick_date'], 'weekly': data['weekly']['pick_date'], 'monthly_symbols': [r.get('symbol') for r in data['monthly']['rows']], 'weekly_symbols': [r.get('symbol') for r in data['weekly']['rows']]}, ensure_ascii=False, indent=2))
    elif args.mode == 'screenshot':
        shots = capture_logged_in_screenshots(['monthly', 'weekly'])
        print(json.dumps({k: str(v) for k, v in shots.items()}, ensure_ascii=False, indent=2))
    else:
        run_check(force=args.force, no_random=args.no_random)


if __name__ == '__main__':
    main()
