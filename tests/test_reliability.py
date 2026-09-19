"""Offline regressions. Network/SDK boundaries are simulated; no API calls are made."""
import asyncio
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import uq_echo_ai as engine
from runtime_support import TaskControl, TaskStopped, valid_mp4, valid_vtt, atomic_text, cache_matches, save_cached
from app_gui import QueueWriter


def box(kind, payload=b'1234'):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


MP4 = box(b'ftyp', b'isom0000') + box(b'moov') + box(b'mdat', b'12345678')
VTT = 'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nclass Example implements Iterable\n'


class Sandbox:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'COURSE' / '2026-07-31'
        self.folder.mkdir(parents=True)
        self.video = self.folder / 'lecture_lower.mp4'
        self.video.write_bytes(MP4)
        self.vtt = self.folder / 'transcript.vtt'
        self.vtt.write_text(VTT)
        self.cfg = json.loads(engine.DEFAULT_CONFIG_PATH.read_text())
        self.cfg.update(course_code='COURSE', output_root=str(self.root), output_dir=str(self.folder.parent))
        self.control = TaskControl()
        self.patches = [patch.object(engine, 'CONTROL', self.control), patch.object(engine, 'APP_HOME', self.root),
                        patch.object(engine, 'CONFIG_PATH', self.root / 'config.json')]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

class EngineRegressions(Sandbox, unittest.TestCase):
    def test_save_config_roundtrip_and_previous_backup(self):
        engine.save_config({'model': 'custom-model'})
        engine.save_config({'model': 'second-model'})
        loaded = engine.load_config()
        self.assertEqual(loaded['model'], 'second-model')
        self.assertEqual(loaded['video_max_frames'], 0)
        self.assertEqual(json.loads((self.root / 'config.json.bak').read_text())['model'], 'custom-model')

    def test_corrupt_config_is_reported_without_overwriting(self):
        path = self.root / 'config.json'
        path.write_text('{broken')
        with self.assertRaises(ValueError):
            engine.load_config()
        self.assertEqual(path.read_text(), '{broken')

    def test_container_validation_rejects_partial_and_html(self):
        self.assertTrue(valid_mp4(self.video))
        self.video.write_bytes(MP4[:-2])
        self.assertFalse(valid_mp4(self.video))
        self.video.write_text('<html>please sign in</html>')
        self.assertFalse(valid_mp4(self.video))

    def test_partial_video_never_counts_as_existing(self):
        self.video.unlink()
        self.video.with_suffix('.mp4.part').write_bytes(MP4)
        self.assertIsNone(engine.existing_video(self.folder))

    def test_vtt_short_timestamps_and_bom(self):
        self.vtt.write_text('\ufeffWEBVTT\n\n01:00.000 --> 01:02.000\nHello\n')
        self.assertTrue(valid_vtt(self.vtt))
        self.assertEqual(engine.parse_vtt_cues(self.vtt)[0]['start'], 60)
        self.vtt.write_text('WEBVTT\n')
        self.assertFalse(valid_vtt(self.vtt))

    def test_cache_checks_output_integrity_and_settings(self):
        out = self.folder / 'summary.md'
        save_cached(out, 'Complete notes', 'key-one')
        self.assertTrue(cache_matches(out, 'key-one'))
        self.assertFalse(cache_matches(out, 'key-two'))
        out.write_text('Complete')
        self.assertFalse(cache_matches(out, 'key-one'))
        save_cached(out, 'New notes', 'key-two')
        self.assertEqual((self.folder / 'summary.previous.md').read_text(), 'Complete')

    def test_changing_model_frames_or_transcript_invalidates_visual_cache(self):
        initial = engine.visual_key(self.video, self.vtt, self.cfg)
        for field, value in [('model', 'another-model'), ('vision_model', 'another-vision'), ('video_max_frames', 20)]:
            self.assertNotEqual(initial, engine.visual_key(self.video, self.vtt, {**self.cfg, field: value}))
        self.vtt.write_text(VTT + '\nnew words')
        self.assertNotEqual(initial, engine.visual_key(self.video, self.vtt, self.cfg))

    def test_empty_or_incomplete_summary_is_not_saved(self):
        out = self.folder / 'summary.md'
        for response in [SimpleNamespace(status='completed', output_text=''), SimpleNamespace(status='incomplete', output_text='Partial answer')]:
            client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
            with patch.object(engine, '_client_from_env', return_value=client):
                self.assertFalse(engine.summarize_with_ai(self.vtt, out, self.cfg, self.folder.name))
            self.assertFalse(out.exists())
        self.assertTrue((self.folder / 'AI_INPUT.md').is_file())

    def test_vision_failure_does_not_make_text_only_summary(self):
        with patch.object(engine, 'analyze_video_with_ai', return_value=None), patch.object(engine, 'summarize_with_ai') as summary:
            self.assertFalse(engine.analyze_folder(self.folder, self.cfg))
            summary.assert_not_called()
        state = json.loads((self.folder / 'status.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertFalse((self.folder / 'done.json').exists())

    def test_analysis_resumes_only_failed_batches(self):
        manifest = []
        for i in range(3):
            image = self.folder / f'frame{i}.jpg'
            image.write_bytes(b'fake-image-used-only-by-mocked-api')
            manifest.append({'file': image, 'index': i+1, 'timestamp': f'00:00:0{i}', 'context': 'class'})
        cfg = {**self.cfg, 'vision_batch_size': 1}
        success = SimpleNamespace(status='completed', output_text='Verified screen content')
        create = Mock(side_effect=[success, RuntimeError('connection interrupted')])
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(engine, 'extract_visual_frames', return_value=(manifest, self.folder / 'visual_manifest.md')), patch.object(engine, '_client_from_env', return_value=client):
            self.assertIsNone(engine.analyze_video_with_ai(self.video, self.vtt, self.folder, cfg))
            self.assertFalse((self.folder / 'visual_analysis.md').exists())
            create.reset_mock(side_effect=True)
            create.return_value = success
            result = engine.analyze_video_with_ai(self.video, self.vtt, self.folder, cfg)
            self.assertEqual(create.call_count, 2)  # First batch was already paid for and saved.
            self.assertTrue(cache_matches(result, engine.visual_key(self.video, self.vtt, cfg)))
            create.reset_mock()
            engine.analyze_video_with_ai(self.video, self.vtt, self.folder, cfg)
            create.assert_not_called()

    def test_stop_after_api_response_does_not_publish_a_result(self):
        def response(**kwargs):
            self.control.stop()
            return SimpleNamespace(status='completed', output_text='answer')
        client = SimpleNamespace(responses=SimpleNamespace(create=response))
        with patch.object(engine, '_client_from_env', return_value=client):
            with self.assertRaises(TaskStopped):
                engine.summarize_with_ai(self.vtt, self.folder / 'summary.md', self.cfg, self.folder.name)
        self.assertFalse((self.folder / 'summary.md').exists())

    def test_unlimited_frames_can_exceed_36_and_small_cap_is_honored(self):
        cues = [{'start': float(i), 'end': float(i+1), 'text': 'class code'} for i in range(0, 600, 10)]
        cfg = {**self.cfg, 'video_max_frames': 0, 'video_min_frame_gap_sec': 5}
        with patch.object(engine, 'parse_vtt_cues', return_value=cues), patch.object(engine, 'video_duration_seconds', return_value=600), patch.object(engine, 'extract_frame'), patch.object(engine, '_image_difference', return_value=0):
            selected = engine.select_visual_timestamps(self.video, self.vtt, cfg)
            self.assertGreater(len(selected), 36)
            self.assertGreater(max(selected), 550)
            self.assertLessEqual(len(engine.select_visual_timestamps(self.video, self.vtt, {**cfg, 'video_max_frames': 3})), 3)

    def test_term_filter_works_beyond_the_original_semester(self):
        for title in ('2027 S1 UQ Standard', 'INFS2200_S1_2027_STLUCIA', 'Semester 1, 2027'):
            self.assertTrue(engine._matches_term(title, '2027S1'))
        self.assertFalse(engine._matches_term('2027 S2 UQ Standard', '2027S1'))

    def test_clearing_saved_key_clears_old_environment_value(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'old-token'}):
            engine.save_credentials('openai', '')
            self.assertEqual(os.environ['OPENAI_API_KEY'], '')
            self.assertIn('OPENAI_API_KEY=""', (self.root / '.env').read_text())

    def test_logs_redact_credentials_split_across_write_calls(self):
        events = queue.Queue()
        writer = QueueWriter(events)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'demo-sensitive-value'}):
            writer.write('key=demo-sensitive-')
            writer.write('value https://host/asset?Signature=private\n')
        line = events.get()[1]
        self.assertNotIn('demo-sensitive', line)
        self.assertNotIn('Signature=', line)
        self.assertIn('REDACTED', line)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'system ffmpeg unavailable')
    def test_real_mp4_duration_and_frame_extraction(self):
        ffmpeg = shutil.which('ffmpeg')
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:d=3', '-c:v', 'mpeg4', '-y', str(self.video)], check=True, timeout=30)
        self.assertTrue(valid_mp4(self.video))
        with patch.object(engine, 'ffmpeg_exe', return_value=ffmpeg):
            self.assertGreater(engine.video_duration_seconds(self.video), 2)
            frame = self.folder / 'frame.jpg'
            engine.extract_frame(self.video, 1, frame, max_width=160)
            self.assertGreater(frame.stat().st_size, 100)


class AsyncRegressions(Sandbox, unittest.IsolatedAsyncioTestCase):
    async def test_closed_page_download_falls_back_and_promotes_atomically(self):
        target = self.folder / 'new-video.mp4'
        download = SimpleNamespace(url='https://assets.example/download', save_as=AsyncMock(side_effect=RuntimeError('Target page closed')))
        async def fallback(url, context, referer, path):
            path.write_bytes(MP4)
        with patch.object(engine, 'stream_authorized_asset', side_effect=fallback) as stream:
            await engine.save_download(download, target, object(), 'https://echo360.net.au/section/example/home')
            stream.assert_awaited_once()
        self.assertTrue(valid_mp4(target))
        self.assertFalse(list(self.folder.glob('*.part')))

    async def test_bad_fallback_preserves_existing_file_and_removes_partial(self):
        download = SimpleNamespace(url='https://assets.example/download', save_as=AsyncMock(side_effect=RuntimeError('closed')))
        async def fallback(url, context, referer, path):
            path.write_text('<html>login required</html>')
        with patch.object(engine, 'stream_authorized_asset', side_effect=fallback):
            with self.assertRaises(ValueError):
                await engine.save_download(download, self.video, object(), 'https://echo360.net.au/')
        self.assertEqual(self.video.read_bytes(), MP4)
        self.assertFalse(list(self.folder.glob('*.part')))

    async def test_partial_file_does_not_short_circuit_the_download_worker(self):
        self.video.unlink()
        self.video.with_suffix('.mp4.part').write_bytes(MP4)
        page = SimpleNamespace(url='https://echo360.net.au/section/test/home')
        with patch.object(engine, 'open_media_menu', AsyncMock(return_value=None)) as menu:
            self.assertIsNone(await engine._download_lower_video_on_page(page, object(), self.folder))
            menu.assert_awaited_once()

    async def test_worker_never_falls_back_to_clicking_main_class_list(self):
        header = SimpleNamespace(inner_text=AsyncMock(return_value='July 31, 2026 lecture'))
        worker = SimpleNamespace(goto=AsyncMock(), is_closed=Mock(return_value=False), close=AsyncMock())
        main = SimpleNamespace(url='https://echo360.net.au/section/test/home', context=SimpleNamespace(new_page=AsyncMock(return_value=worker)))
        with patch.object(engine, 'find_header', AsyncMock(return_value=None)), patch.object(engine, '_download_lower_video_on_page', AsyncMock()) as click:
            self.assertIsNone(await engine.download_lower_video(main, header, self.folder))
            click.assert_not_awaited()
            worker.close.assert_awaited_once()

    async def test_stop_cancels_browser_task_and_runs_cleanup(self):
        cleaned = []
        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        asyncio.get_running_loop().call_later(0.03, self.control.stop)
        with self.assertRaises(TaskStopped):
            await self.control.run(pending())
        self.assertEqual(cleaned, [True])

    async def test_failed_lecture_does_not_block_the_next_one(self):
        titles = ['July 29, 2026 Lecture A', 'July 31, 2026 Lecture B']
        headers = [SimpleNamespace(inner_text=AsyncMock(return_value=t)) for t in titles]
        page = SimpleNamespace(context=object(), url='https://echo360.net.au/section/x/home', is_closed=Mock(return_value=False), goto=AsyncMock())
        cfg = {**self.cfg, 'section_url': page.url, 'analyze_video': False, 'summarize_with_api': False}
        async def find(page, title):
            return headers[titles.index(title)]
        with patch.object(engine, 'lecture_headers', AsyncMock(return_value=headers)), patch.object(engine, 'menu_opener_for_header', AsyncMock(return_value=object())), patch.object(engine, 'find_header', side_effect=find), patch.object(engine, 'lecture_assets_complete', return_value=False), patch.object(engine, 'process_one', AsyncMock(side_effect=[False, False, True])) as process:
            report = await engine.process_section_page(page, cfg)
            self.assertEqual(process.await_count, 3)
            self.assertEqual(report, {'completed': 1, 'failed': 1, 'skipped': 0})


@unittest.skipUnless(os.name == 'nt' or os.getenv('DISPLAY'), 'GUI requires Windows or a display server')
class WindowsGuiSmoke(unittest.TestCase):
    def test_gui_constructs_and_save_settings_works(self):
        from app_gui import EchoLectureApp
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, 'APP_HOME', Path(tmp)), patch.object(engine, 'CONFIG_PATH', Path(tmp) / 'config.json'), patch.object(engine, 'load_local_env', return_value=(None, {})):
            app = EchoLectureApp()
            try:
                app.update_idletasks()
                self.assertTrue(app.save_settings(quiet=True))
                self.assertTrue((Path(tmp) / 'config.json').exists())
            finally:
                app.destroy()


if __name__ == '__main__':
    unittest.main()
