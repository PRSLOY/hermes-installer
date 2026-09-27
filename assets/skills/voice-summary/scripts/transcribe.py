"""Расшифровать аудиофайл встроенным распознаванием речи Hermes (без ключей).

    python transcribe.py <аудиофайл> [--out <файл.txt>]

Запускать Python'ом из venv Hermes: скрипт берёт модуль распознавания самого
Hermes (tools/transcription_tools.py) и те же настройки `stt`, что и
голосовые в Телеграме. По умолчанию это локальная модель faster-whisper:
звук никуда не отправляется. Текст пишется в файл рядом с аудио
(<имя>.transcript.txt) или в --out; в консоль — сводка и начало текста.
"""
import os
import sys
from pathlib import Path


def hermes_repo():
    here = Path(sys.executable).resolve()
    # <home>/hermes-agent/venv/Scripts/python.exe (Windows) or venv/bin/python
    for parent in here.parents:
        if (parent / 'tools' / 'transcription_tools.py').is_file():
            return parent
    home = Path(os.environ.get('HERMES_HOME') or Path(os.environ.get('LOCALAPPDATA', '')) / 'hermes')
    repo = home / 'hermes-agent'
    return repo if (repo / 'tools' / 'transcription_tools.py').is_file() else None


def main(argv):
    out = None
    if '--out' in argv:
        i = argv.index('--out')
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 1:
        print('Использование: python transcribe.py <аудиофайл> [--out файл.txt]')
        return 2
    audio = Path(argv[0]).expanduser().resolve()
    if not audio.is_file():
        print('Файл не найден: %s' % audio)
        return 2
    repo = hermes_repo()
    if repo is None:
        print('Не нашёл Hermes: запусти скрипт Python\'ом из папки hermes-agent\\venv.')
        return 2
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    try:
        from tools.transcription_tools import transcribe_audio
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        print('Распознавание речи в этом Hermes недоступно (%s). Скажи человеку, что расшифровать не получилось.' % type(exc).__name__)
        return 1
    result = transcribe_audio(str(audio))
    if not result.get('success') or not (result.get('transcript') or '').strip():
        print('Расшифровать не удалось: %s' % (result.get('error') or 'пустой результат (тишина или обрыв записи)'))
        return 1
    text = result['transcript'].strip()
    target = out or audio.with_name(audio.stem + '.transcript.txt')
    target.write_text(text + '\n', encoding='utf-8')
    print('Готово (%s): %d символов, файл %s' % (result.get('provider') or 'stt', len(text), target))
    print('Начало: ' + text[:600] + ('…' if len(text) > 600 else ''))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main(sys.argv[1:]))
