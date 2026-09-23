#!/usr/bin/env python3
"""Выгрузить все HTML-таблицы страницы построчно, с номерами строк.

Запуск:  python dump_html_tables.py <url>

Только стандартная библиотека. Нужен для страниц со справочными таблицами
(например, моменты затяжки), где нужное значение часто стоит в соседней
строке с пометкой исполнения. Читать ВСЕ строки, не фильтровать по слову.
Скрипт только читает страницу и печатает текст, ничего не сохраняет.
"""
import gzip
import html
import re
import sys
import urllib.request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; table-dump/1.0)",
    "Accept-Language": "ru,en;q=0.8",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    page = re.sub(r"(?is)<script.*?</script>", "", fetch(sys.argv[1]))
    tables = re.findall(r"(?is)<table[^>]*>(.*?)</table>", page)
    if not tables:
        print("Таблиц на странице не найдено")
        return
    for ti, table in enumerate(tables):
        print(f"=== ТАБЛИЦА {ti} ===")
        rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table)
        for ri, row in enumerate(rows):
            cells = [
                clean(c)
                for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)
            ]
            print(f"R{ri:02d}: " + " | ".join(cells))


if __name__ == "__main__":
    main()
