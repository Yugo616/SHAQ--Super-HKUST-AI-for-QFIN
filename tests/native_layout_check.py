"""Real desktop layout acceptance; uses isolated data and no external calls.

Run directly with --output report.json. The checks catch text-input styles
leaking onto checkboxes, off-screen controls and narrow-window overflow.
"""
import argparse
import json
import tempfile
import time
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import webview
from shaq_daily_oracle.desktop import launch_desktop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold', type=int, default=0)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    report = {'checks': [], 'failures': []}
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
                for width in (1320, 980):
                    window.resize(width, 760)
                    # Windows counts its native frame in the requested width.
                    wait(f'innerWidth <= {width} && innerWidth >= {width - 40}')
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

    with tempfile.TemporaryDirectory(prefix='shaq-layout-') as directory, patch.object(webview, 'start', start):
        launch_desktop(smoke_output=Path(directory) / 'unused.json')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
