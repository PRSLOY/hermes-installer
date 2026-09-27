"""Запасной поиск без ключей, когда встроенный web_search Hermes недоступен.

    python search.py "запрос" [--n 8]

Сначала DuckDuckGo (HTML-версия), при сбое — Bing. Печатает заголовок, адрес и
фрагмент текста для каждого результата. Только стандартная библиотека Python.
"""
import html
import re
import sys
import urllib.parse
import urllib.request

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0 Safari/537.36')


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'ru,en;q=0.8'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', text))).strip()


def ddg(query, n):
    body = get('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(query))
    out = []
    # Each result starts at its title link; the snippet follows before the next one.
    starts = [m.start() for m in re.finditer(r'class="result__a"', body)] + [len(body)]
    for a, b in zip(starts, starts[1:]):
        block = body[a - 20:b]
        link = re.search(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get('uddg')
        url = target[0] if target else href
        if 'duckduckgo.com/y.js' in url:   # ads
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', block, re.S)
        out.append((clean(link.group(2)), url, clean(snippet.group(1)) if snippet else ''))
        if len(out) >= n:
            break
    return out


def unwrap_bing(url):
    """bing.com/ck/a?...&u=a1<base64url> -> the real address."""
    if '/ck/a?' not in url:
        return url
    u = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('u', [''])[0]
    if u.startswith('a1'):
        import base64
        raw = u[2:] + '=' * (-len(u[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(raw).decode('utf-8', 'replace')
        except ValueError:
            pass
    return url


def bing(query, n):
    body = get('https://www.bing.com/search?setlang=ru&q=' + urllib.parse.quote(query))
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', body, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        out.append((clean(link.group(2)), unwrap_bing(html.unescape(link.group(1))), clean(snippet.group(1)) if snippet else ''))
        if len(out) >= n:
            break
    return out


def main(argv):
    n = 8
    if '--n' in argv:
        i = argv.index('--n')
        n = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    query = ' '.join(argv).strip()
    if not query:
        print('Использование: python search.py "запрос" [--n 8]')
        return 2
    for name, engine in (('DuckDuckGo', ddg), ('Bing', bing)):
        try:
            results = engine(query, n)
        except Exception as exc:  # noqa: BLE001 - any failure moves on to the next engine
            print('%s не ответил (%s), пробую дальше.' % (name, type(exc).__name__))
            continue
        if results:
            print('Поиск: %s, запрос: %s' % (name, query))
            for i, (title, url, snippet) in enumerate(results, 1):
                print('%d. %s\n   %s\n   %s' % (i, title, url, snippet))
            return 0
        print('%s ничего не вернул, пробую дальше.' % name)
    print('Поиск не удался: оба поисковика не ответили. Скажи об этом человеку, не выдумывай результаты.')
    return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main(sys.argv[1:]))
