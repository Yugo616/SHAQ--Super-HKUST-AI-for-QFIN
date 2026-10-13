import json
import threading
import unittest

from shaq_daily_oracle.desktop_activity import DesktopNotifications, event_script


class DesktopNotificationTests(unittest.TestCase):
    def test_changed_delivery_is_prompt_and_unchanged_is_suppressed(self):
        delivered, observed_twice, events = threading.Event(), threading.Event(), []
        reads = []
        def activity(revision):
            reads.append(revision)
            if len(reads) >= 2:
                observed_twice.set()
            return dict(revision='new', changed=revision != 'new', jobs=[])
        def emit(name, value):
            events.append((name, value))
            delivered.set()
        observer = DesktopNotifications(activity, lambda: None, emit, interval=.01)
        observer.start()
        observer.start()
        try:
            self.assertTrue(delivered.wait(5))
            self.assertTrue(observed_twice.wait(5))
        finally:
            observer.close()
        self.assertEqual(len([e for e in events if e[0] == 'shaq:activity']), 1)
        self.assertGreater(len(reads), 1)
        self.assertEqual(reads[0], '')
        self.assertTrue(all(value == 'new' for value in reads[1:]))

    def test_failed_native_delivery_retries_the_queued_revision(self):
        delivered, reads, events = threading.Event(), [], []
        def activity(revision):
            reads.append(revision)
            return dict(revision='next', changed=revision != 'next', jobs=[])
        def emit(name, value):
            if name == 'shaq:activity' and not events:
                events.append('failed')
                raise RuntimeError('window temporarily unavailable')
            if name == 'shaq:activity':
                events.append(name)
                delivered.set()
        observer = DesktopNotifications(activity, lambda: None, emit, interval=.01)
        observer.start()
        try:
            self.assertTrue(delivered.wait(5))
        finally:
            observer.close()
        self.assertEqual(reads[0], '')
        self.assertTrue(all(revision == 'next' for revision in reads[1:]))
        self.assertEqual(events, ['failed', 'shaq:activity'])

    def test_slow_update_state_cannot_hold_analysis_and_close_drains_readers(self):
        update_entered, release, activity_seen, closed = [threading.Event() for _ in range(4)]
        def update():
            update_entered.set()
            release.wait(2)
            return {'status': 'available'}
        observer = DesktopNotifications(
            lambda revision: dict(changed=True, revision='live', jobs=[]), update,
            lambda name, value: activity_seen.set() if name == 'shaq:activity' else None,
            interval=.01)
        observer.start()
        self.assertTrue(update_entered.wait(5))
        self.assertTrue(activity_seen.wait(5))
        closer = threading.Thread(target=lambda: (observer.close(), closed.set()))
        closer.start()
        try:
            self.assertFalse(closed.wait(.03), 'cleanup overtook a native reader')
        finally:
            release.set()
            closer.join(2)
        self.assertTrue(closed.is_set())

    def test_update_snapshot_is_emitted_only_on_change(self):
        events = []
        delivered, observed = threading.Event(), threading.Event()
        reads = []
        def update():
            reads.append(True)
            if len(reads) >= 2:
                observed.set()
            return {'status': 'current'}
        observer = DesktopNotifications(lambda _: {'changed': False},
            update, lambda *args: (events.append(args), delivered.set()), interval=.01)
        observer.start()
        try:
            self.assertTrue(delivered.wait(5))
            self.assertTrue(observed.wait(5))
        finally:
            observer.close()
        self.assertEqual(events, [('shaq:software-update', {'status': 'current'})])

    def test_destroyed_renderer_cannot_hold_file_readers_or_shutdown(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def emit(*args):
            entered.set()
            release.wait(5)
        observer = DesktopNotifications(lambda _: {'changed': True, 'revision': 'new'},
            lambda: None, emit, interval=.01)
        observer.start()
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=lambda: (observer.close(), closed.set()))
        closer.start()
        try:
            self.assertTrue(closed.wait(1), 'renderer wait blocked shutdown')
            self.assertTrue(all(not thread.is_alive() for thread in observer._threads))
        finally:
            release.set()
            closer.join(5)

    def test_event_payload_is_encoded_as_data(self):
        payload = {'text': "');throw new Error('injected');//\n\u2028"}
        script = event_script('shaq:activity', payload)
        encoded = script.split('detail:JSON.parse(', 1)[1].split(')}));', 1)[0]
        self.assertEqual(json.loads(json.loads(encoded)), payload)
