"""Real desktop layout acceptance; uses isolated data and no external calls.

Run directly with --output report.json. The checks catch text-input styles
leaking onto checkboxes, off-screen controls and narrow-window overflow.
"""
import argparse
import json
import os
import tempfile
import time
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import webview
from shaq_daily_oracle.desktop import launch_desktop
from native_geometry import capture_geometry, resize_css_width


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold', type=int, default=0)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'failures': [], 'geometry': []}
    report['application_sha'] = os.environ.get('SHAQ_LAYOUT_APPLICATION_SHA')
    report['validation_sha'] = os.environ.get('SHAQ_LAYOUT_VALIDATION_SHA')
    original_start = webview.start

    def start(_existing_inspector, **kwargs):
        window = webview.windows[-1]
        def wait(expression):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if window.evaluate_js(expression):
                    return
                time.sleep(.1)
            raise AssertionError('UI did not become ready: ' + expression)

        def measure(name, selector):
            value = window.evaluate_js("""((selector)=>{
                const el=document.querySelector(selector), r=el.getBoundingClientRect();
                const checks=[...el.querySelectorAll('input[type=checkbox]')]
                    .filter(x=>x.getClientRects().length).map(x=>{
                        const b=x.getBoundingClientRect();return {id:x.id||x.className,
                            width:b.width,height:b.height};});
                return {width:innerWidth,client:el.clientWidth,scroll:el.scrollWidth,
                    left:r.left,right:r.right,checkboxes:checks};
            })(""" + json.dumps(selector) + ')')
            report['checks'].append({'name': name, **value})
            if args.screenshots and sys.platform == 'darwin':
                args.screenshots.mkdir(parents=True, exist_ok=True)
                subprocess.run(['screencapture', '-x', '-o', '-l', str(window.native.windowNumber()),
                    str(args.screenshots / (name.replace(' ', '-') + '.png'))], check=True)
            if not value['client']:
                report['failures'].append(name + ': test surface not visible')
            if value['scroll'] > value['client'] + 2 or value['right'] > value['width'] + 2 or value['left'] < -2:
                report['failures'].append(name + ': horizontal overflow')
            for box in value['checkboxes']:
                if not (12 <= box['width'] <= 24 and 12 <= box['height'] <= 24):
                    report['failures'].append(name + ': checkbox stretched ' + box['id'])

        def inspect():
            try:
                wait("Boolean(document.querySelector('#run .version-check'))")
                report['initial_geometry'] = capture_geometry(window)
                for width in (1320, 980):
                    resize_css_width(window, width, report['geometry'])
                    for page in ('run', 'editor', 'history'):
                        window.evaluate_js(f"document.querySelector('.nav[data-page={page}]').click()")
                        wait(f"Boolean(document.querySelector('#{page}.active').textContent.trim())")
                        measure(f'{width} {page}', 'main')
                    window.evaluate_js("document.querySelector('.nav[data-page=run]').click(); "
                        "if(document.querySelector('#automatic-panel').classList.contains('hidden')) "
                        "document.querySelector('#edit-automatic').click()")
                    wait("Boolean(document.querySelector('#auto-time'))")
                    measure(f'{width} automatic settings', '#automatic-panel')
                    window.evaluate_js("document.querySelector('#connections-button').click(); "
                        "document.querySelector('#show-api-form').click(); "
                        "document.querySelector('#model-status').textContent='API 错误 ' + 'endpoint/'.repeat(50)")
                    measure(f'{width} connection settings', '#setup .setup-card')
                    window.evaluate_js("document.querySelector('#execution-policy-form').closest('details').open=true")
                    measure(f'{width} advanced execution policy', '#execution-policy-form')
                    window.evaluate_js("document.querySelector('#close-setup').click(); "
                        "document.querySelector('#software-update-button').click()")
                    wait("Boolean(document.querySelector('#automatic-software-update'))")
                    measure(f'{width} software update', '#software-update-modal')
                    window.evaluate_js("document.querySelector('#software-update-modal').close(); "
                        "document.querySelector('.nav[data-page=editor]').click(); "
                        "document.querySelector('#download-methods').click()")
                    wait("Boolean(document.querySelector('.transfer-check'))")
                    measure(f'{width} method download', '#method-transfer-modal')
                    window.evaluate_js("document.querySelector('#method-transfer-close').click()")
                window.evaluate_js("document.querySelector('.nav[data-page=run]').click()")
                wait("Boolean(document.querySelector('[data-progress-retry]'))")
                window.evaluate_js("""
                    document.querySelector('.progress-batch > details').open=true;
                    const select=document.querySelector('[data-research-symbol]');
                    select.value='MSFT';select.dispatchEvent(new Event('change'));
                    document.querySelector('[data-research-section=timeline]').open=true;
                """)
                # Toggle events persist disclosure state before the actual refresh.
                wait("Boolean(wb.researchSelections['fixture-recovery']?.open.includes('timeline'))")
                window.evaluate_js("window.fixtureRefreshed=false;load(false).then(()=>window.fixtureRefreshed=true)")
                wait('window.fixtureRefreshed')
                if not window.evaluate_js("document.querySelector('[data-research-symbol]').value==='MSFT' && "
                    "document.querySelector('.progress-batch > details').open && "
                    "document.querySelector('[data-research-section=timeline]').open && "
                    "document.querySelector('.progress-batch').textContent.includes('实际任务')"):
                    raise AssertionError('Progress refresh changed selection or expansion')
                report['progress_preserved_selection_and_expansion'] = True
                window.evaluate_js("document.querySelector('[data-progress-retry]').click()")
                wait("state.data.fixture_resumed_batch==='fixture-original-batch'")
                report['resume_original_batch_action'] = True
                window.evaluate_js("document.querySelector('#connections-button').click()")
                wait("Boolean(document.querySelector('#execution-policy-form').onsubmit)")
                if not window.evaluate_js("document.querySelector('#execution-policy-form').elements.timeout_seconds.value==='600' && "
                    "document.querySelector('#execution-policy-form').elements.transient_retries.value==='1'"):
                    raise AssertionError('Execution policy defaults differ from the shared policy')
                window.evaluate_js("""
                    const form=document.querySelector('#execution-policy-form');
                    form.elements.timeout_seconds.value=900;form.elements.transient_retries.value=0;
                    form.requestSubmit();
                """)
                wait("document.querySelector('#notice').textContent.includes('调用设置已保存')")
                report['advanced_execution_policy_submit'] = True
                report['status'] = 'failed' if report['failures'] else 'passed'
                if args.hold:
                    window.evaluate_js("document.querySelector('#software-update-button').click()")
                    time.sleep(args.hold)
            except Exception as exc:
                report['status'] = 'failed'
                report['failures'].append(str(exc))
            finally:
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                window.destroy()
        return original_start(inspect, **kwargs)

    # Layout fixtures do not test the updater's persistent background worker.
    # Do not let it recreate admission files while temporary data is removed.
    with tempfile.TemporaryDirectory(prefix='shaq-layout-') as directory, \
            patch.object(webview, 'start', start), \
            patch('shaq_daily_oracle.software_updates.UpdateRuntime.start_automatic_checks'):
        launch_desktop(smoke_output=Path(directory) / 'unused.json')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
