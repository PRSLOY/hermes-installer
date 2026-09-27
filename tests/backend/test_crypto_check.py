"""crypto-check skill: token_check.py turns service answers into the right flags.

No network: fetch() is replaced with canned answers keyed by URL fragment. Shapes
follow the live answers of DexScreener, GoPlus, honeypot.is, RugCheck and tonapi
(checked 2026-09-27 on USDT, PEPE, CAKE, BONK, USDT-TON and fresh pump.fun tokens).
"""
import importlib.util
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'assets' / 'skills' / 'crypto-check' / 'scripts' / 'token_check.py'
spec = importlib.util.spec_from_file_location('token_check', SCRIPT)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)

EVM = '0x1111111111111111111111111111111111111111'
NOW_MS = time.time() * 1000
DAY = 86400 * 1000


def pair(chain, liq, base=EVM, quote='0x2222222222222222222222222222222222222222', age_days=400, fdv=1e6, vol=5e4):
    return {'chainId': chain, 'dexId': 'dex', 'url': 'https://dexscreener.com/x',
            'baseToken': {'address': base, 'name': 'Base', 'symbol': 'BASE'},
            'quoteToken': {'address': quote, 'name': 'Quote', 'symbol': 'QUOTE'},
            'liquidity': {'usd': liq}, 'volume': {'h24': vol}, 'fdv': fdv, 'pairCreatedAt': NOW_MS - age_days * DAY}


def goplus(**fields):
    info = {'is_open_source': '1', 'owner_address': '0x3333333333333333333333333333333333333333'}
    info.update(fields)
    return {'code': 1, 'result': {EVM: info}}


class Case(unittest.TestCase):
    def run_check(self, answers, address=EVM, chain=None):
        def fake(url, timeout=20):
            for fragment, answer in answers.items():
                if fragment in url:
                    return answer
            return None
        original = tc.fetch
        tc.fetch = fake
        try:
            r = tc.run(address, chain)
            return r, tc.render(address, r)
        finally:
            tc.fetch = original


class EvmTests(Case):
    def test_honeypot_is_scam_whatever_else(self):
        r, text = self.run_check({'dexscreener': {'pairs': [pair('bsc', 2e6)]},
                                  'gopluslabs': goplus(is_honeypot='1'),
                                  'honeypot.is': {'honeypotResult': {'isHoneypot': True}}})
        self.assertIn('Итог: Похоже на скам.', text)
        self.assertTrue(any('ХАНИПОТ' in x for x in r.red))

    def test_trusted_stablecoin_admin_powers_are_yellow_not_red(self):
        r, text = self.run_check({'dexscreener': {'pairs': [pair('ethereum', 5e6)]},
                                  'gopluslabs': goplus(trust_list='1', is_blacklisted='1', transfer_pausable='1',
                                                       owner_change_balance='1', is_mintable='1')})
        self.assertEqual(r.red, [])
        self.assertTrue(any('права эмитента' in y for y in r.yellow))
        self.assertIn('Явного скама не видно', text)

    def test_owner_powers_of_an_unknown_token_are_red(self):
        r, text = self.run_check({'dexscreener': {'pairs': [pair('ethereum', 5e6)]},
                                  'gopluslabs': goplus(is_blacklisted='1', slippage_modifiable='1')})
        self.assertTrue(any('поднять налог' in x for x in r.red))
        self.assertIn('Итог: Высокий риск.', text)

    def test_renounced_owner_powers_do_not_count_but_hidden_owner_does(self):
        r, _ = self.run_check({'dexscreener': {'pairs': [pair('ethereum', 5e6)]},
                               'gopluslabs': goplus(owner_address='0x0000000000000000000000000000000000000000',
                                                    is_blacklisted='1', transfer_pausable='1')})
        self.assertEqual(r.red, [])
        self.assertTrue(any('renounced' in g for g in r.good))
        r, _ = self.run_check({'dexscreener': {'pairs': [pair('ethereum', 5e6)]},
                               'gopluslabs': goplus(owner_address='0x0000000000000000000000000000000000000000', hidden_owner='1')})
        self.assertTrue(any('скрытый владелец' in x for x in r.red))

    def test_unlocked_lp_red_when_fresh_yellow_when_old_and_deep(self):
        lp = [{'address': '0x4444444444444444444444444444444444444444', 'percent': '0.9', 'is_locked': 0}]
        fresh, _ = self.run_check({'dexscreener': {'pairs': [pair('bsc', 30000, age_days=2)]},
                                   'gopluslabs': goplus(lp_holders=lp)})
        self.assertTrue(any('ликвидности — её можно вывести' in x for x in fresh.red))
        self.assertTrue(any('меньше недели' in y for y in fresh.yellow))
        old, _ = self.run_check({'dexscreener': {'pairs': [pair('bsc', 3e7, age_days=1200)]},
                                 'gopluslabs': goplus(lp_holders=lp)})
        self.assertFalse(any('ликвидност' in x for x in old.red))
        self.assertTrue(any('не главный риск' in y for y in old.yellow))

    def test_high_tax_red_small_tax_yellow(self):
        r, _ = self.run_check({'dexscreener': {'pairs': [pair('bsc', 2e6)]},
                               'gopluslabs': goplus(buy_tax='0.02', sell_tax='0.25')})
        self.assertTrue(any('продажу 25%' in x for x in r.red))
        self.assertTrue(any('покупку 2.0%' in y for y in r.yellow))

    def test_exchange_wallets_are_not_whales(self):
        holders = [{'address': '0x5%039d' % i, 'percent': '0.08', 'is_contract': 0, 'is_locked': 0, 'tag': 'Binance'} for i in range(8)]
        r, _ = self.run_check({'dexscreener': {'pairs': [pair('ethereum', 5e6)]},
                               'gopluslabs': goplus(holders=holders)})
        self.assertFalse(any('крупнейших' in x for x in r.red + r.yellow))

    def test_chain_is_picked_by_liquidity_on_covered_chains_not_a_fork(self):
        pairs = [pair('pulsechain', 9e6), pair('ethereum', 1e6)]
        r, text = self.run_check({'dexscreener': {'pairs': pairs}, 'gopluslabs': goplus()})
        self.assertIn('сеть ethereum', text)
        self.assertIn('gopluslabs.io/token-security/1/', text)

    def test_quote_side_only_token_takes_its_name_from_the_quote_and_no_foreign_fdv(self):
        other = '0x9999999999999999999999999999999999999999'
        pairs = [pair('ethereum', 4e6, base=other, quote=EVM, fdv=45e6)]
        pairs[0]['quoteToken']['name'] = 'Tether USD'
        r, text = self.run_check({'dexscreener': {'pairs': pairs}, 'gopluslabs': goplus()})
        self.assertIn('Токен: Tether USD', text)
        self.assertNotIn('FDV', text)

    def test_not_traded_anywhere_is_red(self):
        r, _ = self.run_check({'dexscreener': {'pairs': []}, 'gopluslabs': goplus()})
        self.assertTrue(any('не торгуется' in x for x in r.red))

    def test_silent_services_give_an_incomplete_verdict_not_a_clean_one(self):
        r, text = self.run_check({})
        self.assertIn('Проверка неполная', text)
        self.assertIn('Не проверено:', text)


class OtherChainTests(Case):
    SOL = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'
    TON = 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs'

    def test_solana_rugcheck_danger_and_freeze_authority(self):
        r, text = self.run_check({'dexscreener': {'pairs': [pair('solana', 20000, base=self.SOL, age_days=1)]},
                                  'rugcheck': {'risks': [{'name': 'Freeze Authority still enabled', 'description': 'Tokens can be frozen', 'level': 'danger'}], 'lpLockedPct': 0},
                                  'solana/token_security': {'result': {self.SOL: {'freezable': {'status': '1'}}}}},
                                 address=self.SOL)
        self.assertIn('Итог: Похоже на скам.', text)
        self.assertTrue(any('заморозить' in x for x in r.red))

    def test_ton_blacklist_is_scam(self):
        r, text = self.run_check({'dexscreener': {'pairs': [pair('ton', 5e5, base=self.TON)]},
                                  'tonapi': {'verification': 'blacklist', 'metadata': {'name': 'Fake', 'symbol': 'F'}}},
                                 address=self.TON)
        self.assertIn('Итог: Похоже на скам.', text)

    def test_address_kinds_and_garbage(self):
        self.assertEqual(tc.detect(EVM, None), 'evm')
        self.assertEqual(tc.detect(self.SOL, None), 'solana')
        self.assertEqual(tc.detect(self.TON, None), 'ton')
        self.assertIsNone(tc.detect('PEPE', None))
        self.assertEqual(tc.detect(EVM, 'BNB'), 'bsc')
        self.assertEqual(tc.main(['PEPE']), 2)


class SkillTextTests(unittest.TestCase):
    def test_skill_never_asks_for_a_seed_and_runs_the_hermes_python(self):
        text = (SCRIPT.parents[1] / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('Никогда не проси сид-фразу', text)
        self.assertIn('hermes-agent/venv/Scripts/python.exe', text)
        self.assertIn('skills/crypto-check/scripts/token_check.py', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
