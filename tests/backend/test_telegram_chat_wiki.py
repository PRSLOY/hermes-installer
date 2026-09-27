"""telegram-chat-wiki skill: split_export.py turns a Telegram Desktop export into day files.

Fixtures follow the Telegram Desktop export formats: result.json («Машиночитаемый
JSON», single chat or whole account) and messages*.html. The HTML shape is written
from the known export layout, not from a captured real export (not verified live).
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'assets' / 'skills' / 'telegram-chat-wiki' / 'scripts' / 'split_export.py'
spec = importlib.util.spec_from_file_location('split_export', SCRIPT)
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)

CHAT = {
    'name': 'Коля', 'type': 'personal_chat', 'id': 42,
    'messages': [
        {'id': 1, 'type': 'service', 'date': '2026-05-21T09:00:00', 'actor': 'Коля', 'action': 'phone_call'},
        {'id': 2, 'type': 'message', 'date': '2026-05-21T10:15:00', 'from': 'Коля', 'text': 'Привет! Глянь сайт'},
        {'id': 3, 'type': 'message', 'date': '2026-05-21T10:16:30', 'from': 'Павел',
         'text': ['Смотри ', {'type': 'text_link', 'text': 'тут', 'href': 'https://example.com'}, ' — договорились на пятницу']},
        {'id': 4, 'type': 'message', 'date': '2026-05-22T08:00:00', 'from': 'Коля', 'media_type': 'voice_message',
         'file': 'voice_messages/audio_1.ogg', 'text': ''},
        {'id': 5, 'type': 'message', 'date': '2026-05-22T08:05:00', 'from': 'Павел', 'photo': 'photos/p.jpg',
         'text': 'скрин', 'forwarded_from': 'Канал про крипту'},
    ]}

HTML = '''<div class="page_header"><div class="content"><div class="text bold">
Коля
</div></div></div>
<div class="message service" id="message-1"><div class="body details">21 May 2026</div></div>
<div class="message default clearfix" id="message2">
 <div class="body">
  <div class="pull_right date details" title="21.05.2026 10:15:00 UTC+03:00">10:15</div>
  <div class="from_name">
Коля
  </div>
  <div class="text">
Привет!<br>Глянь <a href="https://example.com">сайт</a>
  </div>
 </div>
</div>
<div class="message default clearfix joined" id="message3">
 <div class="body">
  <div class="pull_right date details" title="21.05.2026 10:16:00 UTC+03:00">10:16</div>
  <div class="text">
И ещё одно
  </div>
 </div>
</div>
<div class="message default clearfix" id="message4">
 <div class="body">
  <div class="pull_right date details" title="23.05.2026 21:00:05 UTC+03:00">21:00</div>
  <div class="from_name">Павел</div>
  <div class="media_wrap clearfix"><a class="media clearfix pull_left block_link media_voice_message" href="voice_messages/a.ogg"></a></div>
 </div>
</div>
'''


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = se.main(argv)
    return code, buf.getvalue()


class SplitExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='тг-экспорт ')   # Cyrillic + space on purpose
        self.root = Path(self.tmp.name)
        self.wiki = self.root / 'wiki'

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_single_chat_to_day_files(self):
        export = self.root / 'ChatExport' / 'result.json'
        export.parent.mkdir()
        export.write_text(json.dumps(CHAT, ensure_ascii=False), encoding='utf-8')
        code, out = run([str(export.parent), '--wiki', str(self.wiki), '--slug', 'kolya'])
        self.assertEqual(code, 0, out)
        self.assertIn('Сообщений: 4, дней: 2', out)
        self.assertIn('Период: 2026-05-21 — 2026-05-22', out)
        day1 = (self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-21.md').read_text(encoding='utf-8')
        self.assertIn('date: 2026-05-21', day1)
        self.assertIn('- 10:15 **Коля**: Привет! Глянь сайт', day1)
        self.assertIn('тут (https://example.com) — договорились на пятницу', day1)
        self.assertNotIn('phone_call', day1)   # service records are not messages
        day2 = (self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-22.md').read_text(encoding='utf-8')
        self.assertIn('**Коля**: [голосовое]', day2)
        self.assertIn('[переслано от Канал про крипту] [фото] скрин', day2)

    def test_reimport_rebuilds_the_same_files(self):
        export = self.root / 'result.json'
        export.write_text(json.dumps(CHAT, ensure_ascii=False), encoding='utf-8')
        run([str(export), '--wiki', str(self.wiki), '--slug', 'kolya'])
        first = (self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-21.md').read_bytes()
        run([str(export), '--wiki', str(self.wiki), '--slug', 'kolya'])
        self.assertEqual((self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-21.md').read_bytes(), first)

    def test_whole_account_export_needs_the_chat_name(self):
        export = self.root / 'result.json'
        export.write_text(json.dumps({'chats': {'list': [CHAT, dict(CHAT, name='Маша', messages=[])]}}, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(SystemExit) as ctx:
            run([str(export), '--wiki', str(self.wiki), '--slug', 'kolya'])
        self.assertIn('Коля', str(ctx.exception))
        code, out = run([str(export), '--wiki', str(self.wiki), '--slug', 'kolya', '--chat', 'коля'])
        self.assertEqual(code, 0, out)

    def test_html_export_keeps_author_of_joined_messages(self):
        folder = self.root / 'ChatExport_html'
        folder.mkdir()
        (folder / 'messages.html').write_text(HTML, encoding='utf-8')
        code, out = run([str(folder), '--wiki', str(self.wiki), '--slug', 'kolya'])
        self.assertEqual(code, 0, out)
        self.assertIn('Чат: Коля', out)
        day = (self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-21.md').read_text(encoding='utf-8')
        self.assertIn('- 10:15 **Коля**: Привет!\n  Глянь сайт', day)
        self.assertIn('- 10:16 **Коля**: И ещё одно', day)
        day3 = (self.wiki / 'raw' / 'dialogues' / 'kolya' / '2026-05-23.md').read_text(encoding='utf-8')
        self.assertIn('**Павел**: [голосовое]', day3)

    def test_bad_slug_and_wrong_folder_are_refused(self):
        code, out = run([str(self.root), '--wiki', str(self.wiki), '--slug', 'Коля'])
        self.assertEqual(code, 2)
        with self.assertRaises(SystemExit):
            run([str(self.root), '--wiki', str(self.wiki), '--slug', 'kolya'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
