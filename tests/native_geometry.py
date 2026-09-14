"""Native acceptance sizing in CSS pixels, with retained host diagnostics."""
import json
import math
import sys
import time


def capture_geometry(window):
    geometry = window.evaluate_js("""(()=>({inner_width:innerWidth,inner_height:innerHeight,
        outer_width:outerWidth,outer_height:outerHeight,dpr:devicePixelRatio,
        screen:{width:screen.width,height:screen.height,avail_width:screen.availWidth,
            avail_height:screen.availHeight},visual_viewport:window.visualViewport?
            {width:visualViewport.width,height:visualViewport.height,scale:visualViewport.scale}:null}))()""")
    geometry.update(native_width=window.width, native_height=window.height, resize_scale=1)
    if sys.platform == 'win32':
        from System.Windows.Forms import Screen
        form = window.native
        area = Screen.FromControl(form).WorkingArea
        geometry.update(native_width=int(form.Width), native_height=int(form.Height),
            native_client_width=int(form.ClientSize.Width), native_client_height=int(form.ClientSize.Height),
            device_dpi=int(form.DeviceDpi), resize_scale=geometry['dpr'],
            native_scale_factor=float(form.scale_factor),
            work_area={'x': int(area.X), 'y': int(area.Y), 'width': int(area.Width), 'height': int(area.Height)})
    return geometry


def resize_css_width(window, target, snapshots, *, capture=capture_geometry, pause=time.sleep):
    # Six native requests maximum; never alter WebView zoom or relax CSS targets.
    for attempt in range(7):
        geometry = capture(window)
        snapshots.append({'target_css_width': target, 'attempt': attempt, 'geometry': geometry})
        actual = geometry['inner_width']
        if abs(actual-target) <= 2:
            return geometry
        if attempt == 6:
            break
        scale = geometry['resize_scale']
        if not math.isfinite(scale) or scale <= 0:
            raise AssertionError('Invalid native resize scale: ' + json.dumps(geometry))
        width = round(geometry['native_width'] + (target-actual)*scale)
        height = round(geometry['native_height'] + (760-geometry['inner_height'])*scale)
        window.resize(width, height)
        pause(.5)
    raise AssertionError(f'CSS width {target} could not be reached; actual {actual}; '
                         + json.dumps(geometry, sort_keys=True))
