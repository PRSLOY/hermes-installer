"""Проверка токена по адресу контракта: красные флаги из открытых сервисов без ключей.

    python token_check.py <адрес> [--chain eth|bsc|base|arbitrum|polygon|optimism|avalanche|solana|ton]

Источники (все бесплатные, без регистрации): DexScreener (торги и ликвидность),
GoPlus (права владельца, налоги, ханипот; EVM и Solana), honeypot.is (симуляция
покупки и продажи; Ethereum, BSC, Base), RugCheck (Solana), tonapi (TON).
Печатает короткий отчёт на русском. Если сервис не ответил, так и пишет:
«не проверено» не значит «чисто». Только стандартная библиотека Python.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (token_check; Hermes)', 'Accept': 'application/json'}
EVM_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')
SOL_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
TON_RE = re.compile(r'^(?:[EU]Q[A-Za-z0-9_-]{46}|-?[0-9]:[0-9a-fA-F]{64})$')
# DexScreener chainId -> GoPlus chain id (EVM) / honeypot.is chainID.
GOPLUS_CHAIN = {'ethereum': '1', 'bsc': '56', 'base': '8453', 'arbitrum': '42161', 'polygon': '137',
                'optimism': '10', 'avalanche': '43114'}
HONEYPOT_CHAIN = {'ethereum': '1', 'bsc': '56', 'base': '8453'}
ALIASES = {'eth': 'ethereum', 'bnb': 'bsc', 'arb': 'arbitrum', 'matic': 'polygon', 'op': 'optimism',
           'avax': 'avalanche', 'sol': 'solana'}


def fetch(url, timeout=20):
    """JSON or None. Never raises: a dead service is reported, not fatal."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 400):
                return None
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1.5)
    return None


def num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def flag(value):
    return str(value) == '1'


class Report:
    def __init__(self):
        self.red, self.yellow, self.good, self.facts, self.unchecked, self.links = [], [], [], [], [], []
        # Traded for months with a deep pool: an unlocked LP is a weaker signal there.
        self.established = False
        # On a trusted list (big stablecoins etc.): admin powers are the issuer's, not a rug.
        self.trusted = False
        self.named = False


def detect(address, chain):
    if chain:
        chain = ALIASES.get(chain.lower(), chain.lower())
        return chain
    if EVM_RE.match(address):
        return 'evm'
    if TON_RE.match(address):
        return 'ton'
    if SOL_RE.match(address):
        return 'solana'
    return None


def dex_pairs(address):
    data = fetch('https://api.dexscreener.com/latest/dex/tokens/' + urllib.parse.quote(address))
    if data is None:
        return None
    return [p for p in (data.get('pairs') or []) if isinstance(p, dict)]


def check_market(pairs, chain, r, address='', now=None):
    now = now or time.time()
    if pairs is None:
        r.unchecked.append('торги и ликвидность (DexScreener не ответил)')
        return chain
    if chain not in ('evm', None):
        pairs = [p for p in pairs if p.get('chainId') == chain] or pairs
    if not pairs:
        r.red.append('Токен не торгуется ни на одной бирже, которую видит DexScreener: продать его, скорее всего, негде.')
        return chain
    if chain == 'evm':
        # The same 0x address can exist on forks (PulseChain copies Ethereum): pick the
        # chain by total liquidity, preferring chains the security services cover.
        totals = {}
        for p in pairs:
            totals[p.get('chainId')] = totals.get(p.get('chainId'), 0) + num((p.get('liquidity') or {}).get('usd'), 0)
        covered = {c: v for c, v in totals.items() if c in GOPLUS_CHAIN}
        chain = max(covered or totals, key=lambda c: (covered or totals)[c])
        pairs = [p for p in pairs if p.get('chainId') == chain]
    # /tokens also returns pairs where the token is only the QUOTE side (USDT is quoted
    # everywhere); name and pool must come from pairs where it is the base token.
    own = [p for p in pairs if ((p.get('baseToken') or {}).get('address') or '').lower() == address.lower()]
    best = max(pairs, key=lambda p: num((p.get('liquidity') or {}).get('usd'), 0))
    if chain == 'evm':
        chain = best.get('chainId')
    named = max(own, key=lambda p: num((p.get('liquidity') or {}).get('usd'), 0)) if own else best
    base = (named.get('baseToken') or {}) if own else (
        best.get('baseToken') if ((best.get('baseToken') or {}).get('address') or '').lower() == address.lower() else best.get('quoteToken')) or {}
    r.named = True
    # Everything tradable counts: a stablecoin sits mostly on the quote side of pools.
    liq = sum(num((p.get('liquidity') or {}).get('usd'), 0) for p in pairs)
    # Volume and FDV describe the pair's BASE token: take them only from our own pairs.
    vol = sum(num((p.get('volume') or {}).get('h24'), 0) for p in pairs)
    fdv = num(named.get('fdv')) if own else None
    # The oldest pool approximates the token's age.
    ages = [num(p.get('pairCreatedAt')) for p in pairs if num(p.get('pairCreatedAt'))]
    created = min(ages) if ages else None
    r.facts.append('Токен: %s (%s), сеть %s, биржа %s.' % (base.get('name', '?'), base.get('symbol', '?'), best.get('chainId'), best.get('dexId')))
    r.facts.append('Ликвидность во всех пулах: $%s; объём торгов за сутки: $%s%s.' % (
        '{:,.0f}'.format(liq).replace(',', ' '), '{:,.0f}'.format(vol).replace(',', ' '),
        ('; оценка капитализации (FDV): $' + '{:,.0f}'.format(fdv).replace(',', ' ')) if fdv else ''))
    if liq < 10000:
        r.red.append('Ликвидность меньше $10 000: крупную сумму не продать без сильного падения цены, а вытащить такой пул создателю ничего не стоит.')
    elif liq < 50000:
        r.yellow.append('Ликвидность небольшая (меньше $50 000).')
    if created:
        days = (now - created / 1000) / 86400
        r.facts.append('Самому старому пулу %s' % (('%.0f дн.' % days) if days >= 1 else ('%.0f ч.' % (days * 24))))
        r.established = days >= 180 and liq >= 250000
        if days < 7:
            r.yellow.append('Пулу меньше недели: большинство рагпулов случается именно в первые дни.')
    if best.get('url'):
        r.links.append(best['url'])
    return chain


def check_goplus_evm(address, chain, r):
    cid = GOPLUS_CHAIN.get(chain)
    if not cid:
        r.unchecked.append('права владельца и налоги (GoPlus не поддерживает сеть %s)' % chain)
        return
    data = fetch('https://api.gopluslabs.io/api/v1/token_security/%s?contract_addresses=%s' % (cid, address.lower()))
    info = ((data or {}).get('result') or {}).get(address.lower())
    if not info:
        r.unchecked.append('права владельца и налоги (GoPlus не ответил или не знает токен)')
        return
    r.links.append('https://gopluslabs.io/token-security/%s/%s' % (cid, address))
    if flag(info.get('is_honeypot')):
        r.red.append('GoPlus: ХАНИПОТ — купить можно, продать нельзя.')
    if flag(info.get('cannot_sell_all')):
        r.red.append('GoPlus: нельзя продать всё сразу.')
    for key, text in (('buy_tax', 'покупку'), ('sell_tax', 'продажу')):
        tax = num(info.get(key))
        if tax is not None and tax >= 0.1:
            r.red.append('GoPlus: налог на %s %.0f%%.' % (text, tax * 100))
        elif tax is not None and tax > 0:
            r.yellow.append('GoPlus: налог на %s %.1f%%.' % (text, tax * 100))
    if info.get('is_open_source') == '0':
        r.red.append('GoPlus: код контракта не опубликован — что он делает, проверить нельзя.')
    r.trusted = flag(info.get('trust_list'))
    if r.trusted:
        r.good.append('GoPlus: токен в списке доверенных (крупные известные проекты и стейблкоины).')
    owner = (info.get('owner_address') or '').lower()
    renounced = owner in ('', '0x0000000000000000000000000000000000000000', '0x000000000000000000000000000000000000dead')
    if owner and renounced:
        r.good.append('GoPlus: владелец отказался от прав (renounced).')
    elif owner:
        r.facts.append('У контракта есть владелец: %s.' % owner)
    # These work even after a «renounce»: always red.
    for key, text in (('hidden_owner', 'у контракта скрытый владелец'),
                      ('can_take_back_ownership', 'владелец может вернуть себе права после «отказа»'),
                      ('selfdestruct', 'контракт можно уничтожить')):
        if flag(info.get(key)):
            r.red.append('GoPlus: ' + text + '.')
    # Owner powers: moot once renounced; for a trusted issuer (USDT and the like) they are
    # the issuer's compliance controls, a risk to know about but not a rug signal.
    powers = [text for key, text in (
        ('owner_change_balance', 'менять балансы держателей'),
        ('is_blacklisted', 'заблокировать продажу конкретным адресам'),
        ('transfer_pausable', 'остановить переводы'),
        ('slippage_modifiable', 'поднять налог в любой момент'),
        ('personal_slippage_modifiable', 'задать налог отдельным адресам'),
        ('is_mintable', 'допечатывать токены')) if flag(info.get(key))]
    if powers and not renounced:
        text = 'GoPlus: владелец может ' + ', '.join(powers) + '.'
        if r.trusted:
            r.yellow.append(text + ' У крупных доверенных токенов (стейблкоины, токены бирж) это обычно права эмитента, а не признак скама.')
        elif powers == ['допечатывать токены']:
            r.yellow.append(text)
        else:
            r.red.append(text)
    if flag(info.get('is_proxy')):
        r.yellow.append('GoPlus: контракт-прокси — логику можно заменить после запуска.')
    creator = num(info.get('creator_percent'), 0) + num(info.get('owner_percent'), 0)
    if creator >= 0.05:
        r.yellow.append('GoPlus: у создателя/владельца %.1f%% всех токенов.' % (creator * 100))
    lp = [h for h in (info.get('lp_holders') or []) if isinstance(h, dict)]
    if lp and not r.trusted:
        locked = sum(num(h.get('percent'), 0) for h in lp if str(h.get('is_locked')) == '1'
                     or (h.get('address') or '').lower().endswith('dead') or (h.get('address') or '') == '0x0000000000000000000000000000000000000000')
        if locked >= 0.5:
            r.good.append('GoPlus: заблокировано или сожжено %.0f%% ликвидности.' % (locked * 100))
        elif r.established:
            r.yellow.append('GoPlus: ликвидность не заблокирована (%.0f%%). У токена, который торгуется давно и с большим пулом, это не главный риск.' % (locked * 100))
        else:
            r.red.append('GoPlus: заблокировано или сожжено только %.0f%% ликвидности — её можно вывести.' % (locked * 100))
    # Exchanges and other labelled entities (tag) are not «whales» that can dump.
    holders = [h for h in (info.get('holders') or []) if isinstance(h, dict)
               and str(h.get('is_locked')) != '1' and str(h.get('is_contract')) != '1' and not h.get('tag')
               and not (h.get('address') or '').lower().endswith('dead')]
    top = sum(num(h.get('percent'), 0) for h in holders[:10])
    if top >= 0.5 and not r.trusted:
        r.red.append('GoPlus: у 10 крупнейших обычных кошельков %.0f%% токенов — они могут обрушить цену.' % (top * 100))
    elif top >= 0.3:
        r.yellow.append('GoPlus: у 10 крупнейших обычных кошельков (без бирж) %.0f%% токенов.' % (top * 100))
    if info.get('holder_count'):
        r.facts.append('Держателей: %s.' % info['holder_count'])


def check_honeypot(address, chain, r):
    cid = HONEYPOT_CHAIN.get(chain)
    if not cid:
        return
    data = fetch('https://api.honeypot.is/v2/IsHoneypot?address=%s&chainID=%s' % (address, cid))
    if not data:
        r.unchecked.append('симуляция покупки и продажи (honeypot.is не ответил)')
        return
    hp = (data.get('honeypotResult') or {}).get('isHoneypot')
    sim = data.get('simulationResult') or {}
    if hp is True:
        r.red.append('honeypot.is: симуляция показала, что продать нельзя (ханипот).')
    elif hp is False and sim:
        r.good.append('honeypot.is: симуляция покупки и продажи прошла (налог покупки %.1f%%, продажи %.1f%%).'
                      % (num(sim.get('buyTax'), 0), num(sim.get('sellTax'), 0)))
    risk = (data.get('summary') or {}).get('risk')
    if risk in ('high', 'very_high', 'honeypot'):
        r.red.append('honeypot.is: общий риск «%s».' % risk)
    r.links.append('https://honeypot.is/?address=%s' % address)


def check_solana(address, r):
    data = fetch('https://api.rugcheck.xyz/v1/tokens/%s/report/summary' % address)
    if data is None:
        r.unchecked.append('RugCheck не ответил')
    else:
        r.links.append('https://rugcheck.xyz/tokens/%s' % address)
        for risk in data.get('risks') or []:
            text = 'RugCheck: %s — %s.' % (risk.get('name', '?'), risk.get('description', '').rstrip('.'))
            (r.red if risk.get('level') == 'danger' else r.yellow).append(text)
        lp = num(data.get('lpLockedPct'))
        if lp is not None:
            if lp >= 50:
                r.good.append('RugCheck: заблокировано %.0f%% ликвидности.' % lp)
            elif r.established:
                r.yellow.append('RugCheck: заблокировано %.0f%% ликвидности; у токена, который торгуется давно и с большим пулом, это не главный риск.' % lp)
            else:
                r.red.append('RugCheck: заблокировано только %.0f%% ликвидности — её можно вывести.' % lp)
    data = fetch('https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=' + address)
    info = ((data or {}).get('result') or {}).get(address)
    if not info:
        r.unchecked.append('права на токен (GoPlus Solana не ответил)')
        return
    for key, text in (('mintable', 'создатель может допечатывать токены'), ('freezable', 'создатель может заморозить ваши токены'),
                      ('balance_mutable_authority', 'кто-то может менять балансы'), ('closable', 'токен-аккаунты можно закрыть')):
        if str((info.get(key) or {}).get('status')) == '1':
            (r.yellow if key == 'mintable' else r.red).append('GoPlus: ' + text + '.')
    fee = info.get('transfer_fee') or {}
    if fee.get('current_fee_rate') not in (None, '', '0', 0):
        r.yellow.append('GoPlus: у токена комиссия на перевод.')


def check_ton(address, r):
    data = fetch('https://tonapi.io/v2/jettons/' + urllib.parse.quote(address))
    if data is None:
        r.unchecked.append('данные контракта TON (tonapi не ответил)')
        return
    meta = data.get('metadata') or {}
    if not r.named:
        r.facts.append('Джеттон: %s (%s).' % (meta.get('name', '?'), meta.get('symbol', '?')))
    verification = data.get('verification')
    if verification == 'blacklist':
        r.red.append('tonapi: токен в чёрном списке (помечен как скам).')
    elif verification == 'whitelist':
        r.good.append('tonapi: токен в белом списке.')
    admin = data.get('admin')
    if admin and data.get('mintable'):
        r.yellow.append('tonapi: у токена есть админ, он может допечатывать токены.')
    if isinstance(admin, dict) and admin.get('is_scam'):
        r.red.append('tonapi: адрес админа помечен как скам.')
    if data.get('holders_count'):
        r.facts.append('Держателей: %s.' % data['holders_count'])
    r.links.append('https://tonviewer.com/' + address)


def verdict(r):
    fatal = any(w in x for x in r.red for w in ('ХАНИПОТ', 'ханипот', 'нельзя продать', 'скрытый владелец', 'чёрном списке', 'помечен как скам'))
    if fatal or len(r.red) >= 2:
        return 'Похоже на скам.'
    if r.red:
        return 'Высокий риск.'
    if r.yellow:
        return 'Явного скама не видно, но есть риски.'
    if r.unchecked:
        return 'Проверка неполная: часть сервисов не ответила. Молчание сервиса не значит, что всё чисто.'
    return 'Автоматическая проверка ничего не нашла. Это не гарантия, что проект честный.'


def run(address, chain=None):
    r = Report()
    kind = detect(address.strip(), chain)
    if kind is None:
        return None
    if kind == 'ton':
        pairs = dex_pairs(address)
        check_market(pairs, 'ton', r, address)
        check_ton(address, r)
    elif kind == 'solana':
        pairs = dex_pairs(address)
        check_market(pairs, 'solana', r, address)
        check_solana(address, r)
    else:
        pairs = dex_pairs(address)
        real = check_market(pairs, kind, r, address)
        if real in (None, 'evm'):
            real = kind if kind != 'evm' else 'ethereum'
            r.facts.append('Сеть не определена по торгам, проверяю как %s.' % real)
        check_goplus_evm(address, real, r)
        check_honeypot(address, real, r)
    return r


def render(address, r):
    lines = ['Адрес: ' + address, 'Итог: ' + verdict(r)]
    for title, items in (('Красные флаги', r.red), ('Жёлтые флаги', r.yellow), ('Хорошие признаки', r.good),
                         ('Факты', r.facts), ('Не проверено', r.unchecked), ('Где посмотреть самому', r.links)):
        if items:
            lines.append(title + ':')
            lines.extend('- ' + i for i in items)
    lines.append('Это автоматическая проверка по открытым данным, а не совет покупать или продавать.')
    return '\n'.join(lines)


def main(argv):
    args = [a for a in argv if not a.startswith('--chain')]
    chain = None
    for i, a in enumerate(argv):
        if a.startswith('--chain='):
            chain = a.split('=', 1)[1]
        elif a == '--chain' and i + 1 < len(argv):
            chain = argv[i + 1]
            args = [x for x in args if x != chain]
    if len(args) != 1:
        print('Использование: python token_check.py <адрес контракта> [--chain eth|bsc|base|solana|ton|...]')
        return 2
    r = run(args[0], chain)
    if r is None:
        print('Это не похоже на адрес контракта (EVM 0x…, Solana или TON). Попросите у человека адрес токена, а не название.')
        return 2
    print(render(args[0], r))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main(sys.argv[1:]))
