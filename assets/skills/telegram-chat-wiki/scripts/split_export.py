"""Разложить экспорт переписки из Telegram Desktop по дням для вики.

    python split_export.py <экспорт> --wiki <папка вики> --slug <kolya> [--chat "Коля"]

<экспорт> — файл result.json (формат «Машиночитаемый JSON»), папка экспорта
(внутри result.json или messages.html, messages2.html, …) или один .html.
Если в result.json экспорт всего аккаунта, нужен --chat с именем чата.

Пишет <вики>/raw/dialogues/<slug>/ГГГГ-ММ-ДД.md — по файлу на каждый день
переписки, сообщения по порядку, без интерпретации. Существующий файл дня
перезаписывается (слой производный, его всегда можно собрать заново).
В конце печатает сводку: участники, даты, сколько сообщений и медиа.
Только стандартная библиотека Python.
"""
import html
import json
import re
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
MEDIA = {'voice_message': 'голосовое', 'video_message': 'видеокружок', 'video_file': 'видео',
         'audio_file': 'аудио', 'sticker': 'стикер', 'animation': 'гифка'}


def flatten(text):
    """JSON «text» is a string or a list of strings and {type, text} parts."""
    if isinstance(text, str):
        return text
    parts = []
    for part in text or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            value = part.get('text', '')
            if part.get('type') == 'text_link' and part.get('href'):
                value = '%s (%s)' % (value, part['href'])
            parts.append(value)
    return ''.join(parts)


def media_note(msg):
    if msg.get('media_type') in MEDIA:
        return '[%s]' % MEDIA[msg['media_type']]
    if msg.get('photo'):
        return '[фото]'
    if msg.get('file'):
        return '[файл: %s]' % Path(str(msg.get('file_name') or msg['file'])).name
    if msg.get('poll'):
        return '[опрос: %s]' % (msg['poll'].get('question') or '')
    if msg.get('location_information'):
        return '[геопозиция]'
    return ''


def from_json(path, chat):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'messages' not in data:
        chats = (data.get('chats') or {}).get('list') or []
        found = [c for c in chats if chat and (c.get('name') or '').strip().lower() == chat.strip().lower()]
        if not found:
            names = ', '.join(sorted({str(c.get('name')) for c in chats if c.get('name')})[:30])
            raise SystemExit('В экспорте несколько чатов. Укажи --chat "Имя". Есть: ' + names)
        data = found[0]
    out = []
    for msg in data.get('messages') or []:
        if msg.get('type') != 'message':
            continue
        when = datetime.fromisoformat(msg['date'])
        text = flatten(msg.get('text')).strip()
        note = media_note(msg)
        fwd = msg.get('forwarded_from')
        body = ' '.join(x for x in (('[переслано от %s]' % fwd) if fwd else '', note, text) if x)
        out.append((when, str(msg.get('from') or msg.get('actor') or '?'), body))
    return data.get('name') or chat or '', out


def from_html(files):
    out, name, author = [], '', '?'
    for f in files:
        page = Path(f).read_text(encoding='utf-8')
        if not name:
            m = re.search(r'<div class="text bold">\s*(.*?)\s*</div>', page, re.S)
            name = html.unescape(m.group(1)).strip() if m else ''
        for block in re.split(r'<div class="message default', page)[1:]:
            date = re.search(r'class="pull_right date details" title="(\d\d)\.(\d\d)\.(\d{4}) (\d\d):(\d\d):(\d\d)', block)
            if not date:
                continue
            d, mo, y, h, mi, s = (int(x) for x in date.groups())
            who = re.search(r'<div class="from_name">\s*(.*?)\s*</div>', block, re.S)
            if who:   # «joined» messages omit the name: same author as before
                author = html.unescape(re.sub(r'<[^>]+>', '', who.group(1))).strip()
            text = re.search(r'<div class="text">\s*(.*?)\s*</div>', block, re.S)
            body = ''
            if text:
                raw = re.sub(r'<br\s*/?>', '\n', text.group(1))
                body = html.unescape(re.sub(r'<[^>]+>', '', raw)).strip()
            if 'media_voice_message' in block:
                body = ('[голосовое] ' + body).strip()
            elif 'class="photo_wrap' in block:
                body = ('[фото] ' + body).strip()
            elif 'class="media clearfix' in block and not body:
                body = '[медиа]'
            out.append((datetime(y, mo, d, h, mi, s), author, body))
    return name, out


def load(source, chat):
    p = Path(source)
    if p.is_dir():
        if (p / 'result.json').is_file():
            return from_json(p / 'result.json', chat)
        pages = sorted(p.glob('messages*.html'), key=lambda f: int(re.sub(r'\D', '', f.stem) or 1))
        if pages:
            return from_html(pages)
        raise SystemExit('В папке нет result.json и messages*.html — это не экспорт Telegram Desktop.')
    if p.suffix.lower() == '.json':
        return from_json(p, chat)
    if p.suffix.lower() in ('.html', '.htm'):
        return from_html([p])
    raise SystemExit('Нужен result.json, messages.html или папка экспорта.')


def write_days(messages, wiki, slug, display, source):
    days = OrderedDict()
    for when, who, body in sorted(messages, key=lambda m: m[0]):
        days.setdefault(when.strftime('%Y-%m-%d'), []).append((when, who, body))
    folder = Path(wiki) / 'raw' / 'dialogues' / slug
    folder.mkdir(parents=True, exist_ok=True)
    for day, items in days.items():
        lines = ['---', 'date: %s' % day, 'dialogue: %s' % slug, 'name: "%s"' % display.replace('"', "'"),
                 'source: "%s"' % Path(source).name, 'messages: %d' % len(items), '---', '',
                 '# %s — %s' % (display or slug, day), '']
        for when, who, body in items:
            text = body.replace('\n', '\n  ') if body else '[пусто]'
            lines.append('- %s **%s**: %s' % (when.strftime('%H:%M'), who, text))
        (folder / (day + '.md')).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return folder, days


def main(argv):
    args, opts = [], {}
    it = iter(argv)
    for a in it:
        if a in ('--wiki', '--slug', '--chat'):
            opts[a[2:]] = next(it, '')
        else:
            args.append(a)
    if len(args) != 1 or 'wiki' not in opts or 'slug' not in opts:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    slug = opts['slug'].strip().lower()
    if not SLUG_RE.match(slug):
        print('slug — латиница, цифры и дефис, например kolya или anna-smm.')
        return 2
    name, messages = load(args[0], opts.get('chat'))
    if not messages:
        print('Сообщений не найдено. Проверь, что это экспорт нужного чата.')
        return 1
    folder, days = write_days(messages, opts['wiki'], slug, name, args[0])
    authors = Counter(who for _, who, _ in messages)
    media = sum(1 for _, _, body in messages if body.startswith('['))
    print('Чат: %s' % (name or '?'))
    print('Сообщений: %d, дней: %d, с медиа: %d' % (len(messages), len(days), media))
    print('Период: %s — %s' % (next(iter(days)), next(reversed(days))))
    print('Участники: ' + ', '.join('%s (%d)' % kv for kv in authors.most_common(10)))
    print('Файлы дней: %s' % folder)
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main(sys.argv[1:]))
