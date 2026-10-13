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
from shaq_daily_oracle.desktop import _bind_gui_smoke_fixture, launch_desktop
from native_geometry import capture_geometry, resize_css_width


def bind_layout_fixture(bridge, fixture_state, fixture_detail):
    # Full-state reads and native activity notifications must share one date.
    # The layout fixture exercises the saved job's day, not the wall clock.
    fixture_state['clock']['et'] = fixture_state['jobs'][0]['started_at_et']
    return _bind_gui_smoke_fixture(bridge, fixture_state, fixture_detail)


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
                if not window.evaluate_js("[...document.querySelectorAll('.version-check')].every(box=>"
                    "box.checked===state.runSelections.has(box.dataset.author+'/'+box.dataset.version))"):
                    raise AssertionError('Visible method selection differs from selected analysis methods')
                report['method_selection_matches_controls'] = True
                report['initial_geometry'] = capture_geometry(window)
                for width in (1320, 980):
                    resize_css_width(window, width, report['geometry'])
                    for page in ('run', 'editor', 'history'):
                        window.evaluate_js(f"document.querySelector('.nav[data-page={page}]').click()")
                        wait(f"Boolean(document.querySelector('#{page}.active').textContent.trim())")
                        measure(f'{width} {page}', 'main')
                        if page == 'history' and not window.evaluate_js("""
                            Boolean(document.querySelector('.balance-overview')) &&
                            Boolean(document.querySelector('.result-table')) &&
                            !/一股|手续费|滑点/.test(document.querySelector('#history').textContent) &&
                            !document.querySelector('#history .balance-overview details')
                        """):
                            raise AssertionError('Results page contains obsolete accounting clutter')
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
                        "document.querySelector('#manual-software-update').click()")
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
                    document.querySelector('[data-progress-variant]').open=true;
                    document.querySelector('[data-research-section="MSFT:price_volume"]').open=true;
                    document.querySelector('#run').style.minHeight='1600px';
                    state.data.jobs[0].message='force a changed refresh';
                    window.fixtureGetState=window.pywebview.api.get_lab_state;
                    window.pywebview.api.get_lab_state=async()=>{
                        const result=await window.fixtureGetState();
                        await new Promise(resolve=>window.fixtureReleaseState=resolve);
                        return result;
                    };
                    window.fixtureRefreshed=false;
                    load(false).then(()=>window.fixtureRefreshed=true);
                """)
                wait('Boolean(window.fixtureReleaseState)')
                # The user keeps reading while a real bridge response is pending.
                window.evaluate_js("window.scrollTo(0,240);window.fixtureScroll=window.scrollY;window.fixtureReleaseState()")
                wait('window.fixtureRefreshed')
                if not window.evaluate_js("document.querySelector('[data-progress-variant]').open && "
                    "document.querySelector('[data-research-section=\"MSFT:price_volume\"]').open && "
                    "document.querySelector('[data-progress-variant] progress').hasAttribute('value') && "
                    "Math.abs(window.scrollY-window.fixtureScroll)<=2"):
                    raise AssertionError('Progress refresh changed reading position or expansion')
                window.evaluate_js("window.pywebview.api.get_lab_state=window.fixtureGetState")
                report['progress_preserved_selection_and_expansion'] = True
                report['pending_refresh_preserved_latest_scroll'] = True
                window.evaluate_js("document.querySelector('[data-progress-retry]').click()")
                wait("state.data.fixture_resumed_batch==='fixture-original-batch'")
                wait("Boolean(document.querySelector('[data-progress-job=resume-fixture-original-batch] progress'))")
                if window.evaluate_js("document.querySelectorAll('[data-progress-job]').length") != 1:
                    raise AssertionError('Recovery duplicated the old failed progress')
                report['resume_original_batch_action'] = True
                report['resume_progress_visible'] = True
                window.evaluate_js("""
                    state.data.jobs=[];state.activityRevision='not-yet-seen';
                    renderRun();window.fixtureDiscovered=false;
                    pollDesktopActivity().then(()=>window.fixtureDiscovered=true);
                """)
                wait('window.fixtureDiscovered')
                wait("Boolean(document.querySelector('[data-progress-job=resume-fixture-original-batch] .overall-progress'))")
                report['external_job_discovery'] = True
                report['backend_total_progress'] = True
                report['progress_node_checks'] = window.evaluate_js("""
                    window.fixtureLiveBar=document.querySelector('.overall-progress progress');
                    window.fixtureLiveSummary=document.querySelector('.overall-progress');
                    window.fixtureLiveDetails=document.querySelector('[data-progress-variant]');
                    fixtureLiveDetails.open=true;
                    const live=JSON.parse(JSON.stringify(state.data.jobs));
                    const visible=live.find(job=>job.job_id===fixtureLiveBar.closest('[data-progress-job]').dataset.progressJob);
                    visible.progress_summary.completed_tasks+=1;
                    visible.progress_summary.total_tasks=Math.max(visible.progress_summary.total_tasks,visible.progress_summary.completed_tasks+1);
                    window.dispatchEvent(new CustomEvent('shaq:activity',{detail:{
                        changed:true,revision:'native-fixture-progress',jobs:live,
                        result_refresh:state.data.result_refresh,clock:state.data.clock,
                        observed_at:state.data.clock.et}}));
                    tickLocalClock(new Date(state.data.clock.et));
                    ({bar:fixtureLiveBar===document.querySelector('.overall-progress progress'),
                      summary:fixtureLiveSummary===document.querySelector('.overall-progress'),
                      details:fixtureLiveDetails===document.querySelector('[data-progress-variant]'),
                      open:fixtureLiveDetails.open,
                      value:fixtureLiveBar.value===visible.progress_summary.completed_tasks});
                """)
                if not all(report['progress_node_checks'].values()):
                    raise AssertionError('Native notification replaced a live progress/control node')
                report['native_notification_retained_progress_nodes'] = True
                report['formal_node_checks'] = window.evaluate_js("""
                    state.data.formal_operator={connected:true,session_date:'fixture',
                        service:{state:'workflow_running'},runs:[]};
                    refreshFormalProgress();
                    window.fixtureFormalLive=document.querySelector('#formal-live');
                    window.fixtureFormalDetail=document.querySelector('#formal-detail');
                    fixtureFormalDetail.textContent='opened report';
                    state.data.formal_operator.service.state='workflow_complete';
                    refreshFormalProgress();
                    const retained={live:fixtureFormalLive===document.querySelector('#formal-live'),
                        detail:fixtureFormalDetail===document.querySelector('#formal-detail'),
                        content:fixtureFormalDetail.textContent==='opened report',
                        status:fixtureFormalLive.textContent.includes('正式流程完成')};
                    state.data.formal_operator.connected=false;refreshFormalProgress();
                    retained.disconnected=!document.querySelector('#formal-operator');
                    retained;
                """)
                if not all(report['formal_node_checks'].values()):
                    raise AssertionError('Formal status update lost the report or retained disconnected controls')
                if not window.evaluate_js("document.querySelector('#software-update-button').hidden"):
                    raise AssertionError('No-release fixture displayed an update button')
                report['no_release_hides_update_button'] = True
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
                    window.evaluate_js("document.querySelector('#manual-software-update').click()")
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
            patch('shaq_daily_oracle.desktop._bind_gui_smoke_fixture', bind_layout_fixture), \
            patch('shaq_daily_oracle.software_updates.UpdateRuntime.start_automatic_checks'):
        launch_desktop(smoke_output=Path(directory) / 'unused.json')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
