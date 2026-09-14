import tempfile
import subprocess
import json
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from shaq_daily_oracle import update_gui
from shaq_daily_oracle.update_admission import AdmissionGate, UpdateBusy, WorkerAdmission


class Window:
    def __init__(self, dirty=False):
        self.dirty, self.frozen, self.closed = dirty, False, False

    def evaluate_js(self, script):
        if 'prepare' in script:
            self.frozen = True
            return not self.dirty
        self.frozen = False

    def destroy(self):
        self.closed = True


class UpdateGuiTests(unittest.TestCase):
    def test_paused_old_bridge_cannot_register_after_target_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=SimpleNamespace(data_root=Path(directory),package_root=Path(__file__).parents[1])
            old=WorkerAdmission(paths)
            gate=old.gate
            with gate.install():gate.mark_installing('0.7.0')
            with gate.target_startup('0.7.0'):
                fresh=WorkerAdmission(paths)
                with update_gui.GuiSession(gate.root,Window(),admission=fresh):pass
                gate.finish_restart('0.7.0')
            with self.assertRaises(UpdateBusy):
                with update_gui.GuiSession(gate.root,Window(),admission=old):pass

    def test_registration_after_participant_discovery_is_rejected_during_install(self):
        with tempfile.TemporaryDirectory() as directory:
            gate=AdmissionGate(Path(directory))
            late=update_gui.GuiSession(gate.root,Window())
            with update_gui.GuiSession(gate.root,Window()) as owner:
                discover=owner._live
                def discover_then_register():
                    participants=discover()
                    with self.assertRaises(UpdateBusy):
                        with late:pass
                    return participants
                with gate.install():
                    gate.mark_installing('9.0.0')
                    with patch.object(owner,'_live',side_effect=discover_then_register):owner.quiesce()
                self.assertNotIn(late.token,owner._live())

    def test_target_window_does_not_consume_previous_generations_close_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, Window()) as owner:
                owner.quiesce()
            target=Window()
            with update_gui.GuiSession(root,target):time.sleep(.15)
            self.assertFalse(target.closed)

    def test_frontend_retains_unsaved_other_provider_and_edits_during_save(self):
        source = Path(__file__).parents[1]/'src/shaq_daily_oracle/desktop/update_exit.js'
        script = "const window={};const document={addEventListener(){},body:{inert:false},querySelector(){}};\n" + source.read_text() + '''
const e=window.SHAQUpdateExit;
e.dirty('connection:first');e.dirty('connection:second');
const saved=e.beforeSave('save_lab_model_profile',[{protocol:'second'}]);
e.afterSave(saved);
const other=e.prepare();e.cancel();
const first=e.beforeSave('save_lab_model_profile',[{protocol:'first'}]);
e.dirty('connection:first');e.afterSave(first);
const newer=e.prepare();e.cancel();
e.afterSave(e.beforeSave('save_lab_model_profile',[{protocol:'first'}]));
const clean=e.prepare();e.cancel();e.dirty('data');
e.afterSave(e.beforeSave('save_lab_setup',[{sec_identity:'synthetic'}]));
const partial=e.prepare();e.cancel();
e.afterSave(e.beforeSave('save_lab_setup',[{data_profile:{}}]));
console.log(JSON.stringify({other,newer,clean,partial,dataSaved:e.prepare(),frozen:document.body.inert}));
'''
        result = subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),dict(other=False,newer=False,clean=True,partial=False,dataSaved=True,frozen=True))

    def test_dirty_other_window_blocks_close_and_unfreezes_every_window(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Window(), Window(True)
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, first) as owner, update_gui.GuiSession(root, second):
                with self.assertRaises(update_gui.UnsavedEdits):owner.quiesce()
                self.assertFalse(first.closed or second.closed)
                self.assertFalse(first.frozen or second.frozen)

    def test_clean_other_window_closes_but_calling_window_waits_for_sdk_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Window(), Window()
            root=AdmissionGate(Path(directory)).root
            with update_gui.GuiSession(root, first) as owner, update_gui.GuiSession(root, second):
                owner.quiesce()
                self.assertTrue(second.closed)
                self.assertFalse(first.closed)
                self.assertTrue(first.frozen)
