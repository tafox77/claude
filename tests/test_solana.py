import base64

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from cryptobot.config import Config, SafetyConfig, SolanaConfig
from cryptobot.safety import DANGER, SAFE, UNKNOWN, SafetyChecker
from cryptobot.solana import (
    INCINERATOR,
    QUOTE_MINTS,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    JupiterBroker,
    SolanaMarketData,
    evaluate_solana_token,
    solana_mint,
)

NOW = 1_760_000_000_000
DAY = 86_400_000
MINT = "MemeCoin1111111111111111111111111111111111"
USDC = QUOTE_MINTS["USDC"]


def mint_account(program=TOKEN_PROGRAM, **info):
    base = {"mintAuthority": None, "freezeAuthority": None, "decimals": 6, "supply": "1000000000000"}
    base.update(info)
    return program, base


def holders():
    return [{"owner": f"w{i}", "percent": 0.02, "is_wallet": True} for i in range(10)]


def pairs(mint=MINT, symbol="MEME", **overrides):
    pair = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "Pool111111111111111111111111111111111111111",
        "baseToken": {"address": mint, "symbol": symbol},
        "quoteToken": {"address": USDC, "symbol": "USDC"},
        "priceUsd": "0.5",
        "priceNative": "0.5",
        "liquidity": {"usd": 2_000_000},
        "volume": {"h24": 5_000_000},
        "txns": {"h24": {"buys": 900, "sells": 800}},
        "pairCreatedAt": NOW - 200 * DAY,
    }
    pair.update(overrides)
    return [pair]


def evaluate(account=None, hold=None, prs=None, rug=None, sell_route=True, impact=0.001, mint=MINT):
    return evaluate_solana_token(
        mint,
        mint_account() if account is None else account,
        holders() if hold is None else hold,
        pairs() if prs is None else prs,
        {"risks": [], "lpLockedPct": 99.0} if rug is None else rug,
        sell_route,
        impact,
        SafetyConfig(),
        now_ms=NOW,
    )


def test_clean_token_is_safe():
    report = evaluate()
    assert report.verdict == SAFE, report.summary()


def test_active_mint_authority_is_danger():
    report = evaluate(mint_account(mintAuthority="Dev1111111111111111111111111111111111111111"))
    assert report.verdict == DANGER
    assert any("print unlimited" in r for r in report.critical)


def test_freeze_authority_is_danger():
    report = evaluate(mint_account(freezeAuthority="Dev1111111111111111111111111111111111111111"))
    assert report.verdict == DANGER
    assert any("freeze" in r for r in report.critical)


@pytest.mark.parametrize(
    "extension",
    [
        {"extension": "permanentDelegate", "state": {"delegate": "Dev1111111111111111111111111111111111111111"}},
        {"extension": "nonTransferable"},
        {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 9000}}},
    ],
)
def test_dangerous_token_2022_extensions(extension):
    report = evaluate(mint_account(TOKEN_2022_PROGRAM, extensions=[extension]))
    assert report.verdict == DANGER, report.summary()


def test_small_transfer_fee_is_only_a_warning():
    ext = {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 100}}}
    report = evaluate(mint_account(TOKEN_2022_PROGRAM, extensions=[ext]))
    assert report.tradable
    assert report.info["transfer_fee"] == "1.00%"


def test_no_sell_route_is_honeypot():
    report = evaluate(sell_route=False)
    assert report.verdict == DANGER
    assert any("sell route" in r for r in report.critical)


def test_rugcheck_danger_risks_block():
    rug = {"risks": [{"name": "Large Amount of LP Unlocked", "level": "danger", "description": "LP can be pulled"}]}
    assert evaluate(rug=rug).verdict == DANGER


def test_unlocked_liquidity_warns():
    report = evaluate(rug={"risks": [], "lpLockedPct": 10})
    assert any("locked" in w for w in report.warnings)


def test_whale_wallets_warn_but_pools_and_burns_dont_count():
    hold = [
        {"owner": "pool", "percent": 0.6, "is_wallet": False},
        {"owner": INCINERATOR, "percent": 0.2, "is_wallet": False, "burned": True},
        *holders(),
    ]
    assert evaluate(hold=hold).verdict == SAFE
    whale = [{"owner": "dev", "percent": 0.45, "is_wallet": True}, *holders()]
    assert any("One wallet holds" in w for w in evaluate(hold=whale).warnings)


def test_impersonated_bonk_is_danger():
    report = evaluate(prs=pairs(symbol="BONK"))
    assert report.verdict == DANGER
    assert any("Impersonation" in r for r in report.critical)


def test_real_usdc_authorities_are_trusted():
    report = evaluate(
        mint_account(mintAuthority="Circle", freezeAuthority="Circle"), prs=pairs(mint=USDC, symbol="USDC"), mint=USDC
    )
    assert report.tradable, report.summary()


def test_pump_fun_bonding_curve_warns():
    report = evaluate(prs=pairs(dexId="pumpfun"))
    assert any("pump.fun" in w for w in report.warnings)


def test_missing_data_fails_closed():
    assert evaluate_solana_token(MINT, None, None, None, None, None, None, SafetyConfig()).verdict == UNKNOWN


def test_not_a_mint_is_danger():
    assert evaluate(account=(SYSTEM_PROGRAM, {})).verdict == DANGER


# ----- end-to-end through SafetyChecker with fake network --------------------------------


class FakeNet:
    def __init__(self, mint_authority=None, route=True):
        self.mint_authority = mint_authority
        self.route = route
        self.calls = []

    def post(self, url, body):
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "getAccountInfo":
            info = {
                "mintAuthority": self.mint_authority,
                "freezeAuthority": None,
                "decimals": 6,
                "supply": "1000000000000",
            }
            return {"result": {"value": {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": info}}}}}
        if method == "getTokenLargestAccounts":
            return {"result": {"value": [{"address": f"acct{i}", "uiAmountString": "20000"} for i in range(5)]}}
        if method == "getMultipleAccounts":
            if params[1]["encoding"] == "jsonParsed":
                return {
                    "result": {"value": [{"data": {"parsed": {"info": {"owner": a + "_owner"}}}} for a in params[0]]}
                }
            return {"result": {"value": [{"owner": SYSTEM_PROGRAM} for _ in params[0]]}}
        raise AssertionError(method)

    def fetch(self, url, params):
        if "dexscreener" in url:
            return {"pairs": pairs()}
        if "rugcheck" in url:
            return {"risks": [], "lpLockedPct": 100}
        if url.endswith("/quote"):
            return {"outAmount": "99", "priceImpactPct": "0.002"} if self.route else {"errorCode": "NO_ROUTE"}
        raise AssertionError(url)


def checker(net):
    return SafetyChecker(SafetyConfig(), fetch=net.fetch, post=net.post, solana=SolanaConfig())


def test_checker_end_to_end_safe():
    net = FakeNet()
    report = checker(net).check_contract("solana", MINT)
    assert report.tradable, report.summary()
    assert report.info["top10_wallets"] == "10.0%"  # 5 wallets x 20k of a 1M supply
    assert "getTokenLargestAccounts" in net.calls


def test_checker_end_to_end_flags_mint_authority_and_honeypot():
    report = checker(FakeNet(mint_authority="Dev", route=False)).check_contract("solana", MINT)
    assert report.verdict == DANGER
    assert len(report.critical) >= 2


def test_checker_rpc_outage_is_unknown():
    def down(url, body):
        raise ConnectionError("rpc down")

    net = FakeNet()
    report = SafetyChecker(SafetyConfig(), fetch=net.fetch, post=down).check_contract("solana", MINT)
    assert report.verdict == UNKNOWN


def test_solana_addresses_keep_their_case():
    net = FakeNet()
    checker(net).check_contract("solana", MINT)
    assert MINT != MINT.lower()  # the lookup would fail if it had been lowercased


# ----- mint resolution and market data ---------------------------------------------------


def test_solana_mint_resolution():
    cfg = Config(venue="solana", symbols=["BONK/USDC"])
    assert solana_mint(cfg, "BONK/USDC") == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert solana_mint(cfg, f"{MINT}/USDC") == MINT
    cfg.token_contracts["MEME/USDC"] = {"chain": "solana", "address": MINT}
    assert solana_mint(cfg, "MEME/USDC") == MINT
    with pytest.raises(ValueError):
        solana_mint(cfg, "NOPE/USDC")


def test_market_data_parses_gecko_candles_and_drops_open_candle():
    now_s = 1_760_000_000
    newest = now_s - now_s % 300  # still-open 5m candle
    candles = [[newest - 300 * i, 1, 2, 0.5, 1.5, 1000] for i in range(5)]  # newest first, like the API
    calls = []

    def fetch(url, params):
        calls.append(url)
        if "dexscreener" in url:
            return {"pairs": pairs(symbol="MEME")}
        return {"data": {"attributes": {"ohlcv_list": candles}}}

    cfg = Config(venue="solana", symbols=[f"{MINT}/USDC"])
    md = SolanaMarketData(cfg, fetch=fetch, clock=lambda: now_s, sleep=lambda s: None)
    rows = md.fetch_closed_ohlcv(f"{MINT}/USDC", "5m", 100)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert rows[-1][0] == (newest - 300) * 1000
    assert len(rows) == 4
    n = len(calls)
    md.fetch_closed_ohlcv(f"{MINT}/USDC", "5m", 100)
    assert len(calls) == n  # cached until the next candle closes


def test_ticker_price_when_token_is_the_quote_side():
    def fetch(url, params):
        return {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "Other1111111111111111111111111111111111111"},
                    "quoteToken": {"address": MINT},
                    "priceUsd": "10",
                    "priceNative": "4",
                    "liquidity": {"usd": 1e6},
                    "volume": {"h24": 3e6},
                }
            ]
        }

    md = SolanaMarketData(Config(venue="solana", symbols=[f"{MINT}/USDC"]), fetch=fetch)
    ticker = md.fetch_ticker(f"{MINT}/USDC")
    assert ticker["last"] == pytest.approx(2.5)
    assert ticker["quoteVolume"] == 3e6


# ----- live Jupiter broker ---------------------------------------------------------------


class FakeChain:
    """Tracks wallet balances and applies a swap when a signed transaction arrives."""

    def __init__(self, owner):
        self.owner = owner
        self.balances = {USDC: 1000.0, MINT: 0.0}
        self.sol = 1.0
        self.pending = None
        self.sent = []

    def mint_info(self, mint):
        return TOKEN_PROGRAM, {"decimals": 6}

    def token_balance(self, owner, mint):
        assert owner == self.owner
        return self.balances[mint]

    def sol_balance(self, owner):
        return self.sol

    def send_transaction(self, tx_b64):
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        assert tx.verify_with_results() == [True]  # signed by our wallet
        self.sent.append(tx)
        src, dst, amount_in, amount_out = self.pending
        self.balances[src] -= amount_in
        self.balances[dst] += amount_out
        return "sig"

    def confirm(self, signature, sleep=None):
        return True


class FakeJupiter:
    def __init__(self, chain, keypair, impact="0.001"):
        self.chain = chain
        self.keypair = keypair
        self.impact = impact

    def quote(self, input_mint, output_mint, amount):
        ui_in = amount / 1e6
        ui_out = ui_in * 2 if input_mint == USDC else ui_in * 0.5  # price $0.50
        self.chain.pending = (input_mint, output_mint, ui_in, ui_out)
        return {"outAmount": str(int(ui_out * 1e6)), "priceImpactPct": self.impact}

    def swap_transaction(self, quote, user):
        msg = MessageV0.try_compile(self.keypair.pubkey(), [], [], Hash.default())
        unsigned = VersionedTransaction.populate(msg, [Signature.default()])  # Jupiter returns it unsigned
        return base64.b64encode(bytes(unsigned)).decode()


def make_broker(impact="0.001"):
    kp = Keypair()
    chain = FakeChain(str(kp.pubkey()))
    cfg = Config(venue="solana", symbols=[f"{MINT}/USDC"])
    md = SolanaMarketData(cfg, fetch=lambda u, p: {})
    broker = JupiterBroker(cfg, md, keypair=kp, rpc=chain, jupiter=FakeJupiter(chain, kp, impact), sleep=lambda s: None)
    return broker, chain


def test_jupiter_broker_buys_and_sells_with_signed_swaps():
    broker, chain = make_broker()
    assert broker.free_cash() == 1000
    qty, price = broker.buy(f"{MINT}/USDC", 200, 0.5)
    assert qty == pytest.approx(200)
    assert price == pytest.approx(0.5)
    assert chain.balances[USDC] == pytest.approx(900)
    qty, price = broker.sell(f"{MINT}/USDC", 500, 0.5)  # asks for more than held: sells what's there
    assert qty == pytest.approx(200)
    assert chain.balances[USDC] == pytest.approx(1000)
    assert len(chain.sent) == 2


def test_jupiter_broker_refuses_high_price_impact_buys():
    broker, chain = make_broker(impact="0.08")
    assert broker.buy(f"{MINT}/USDC", 200, 0.5) == (0.0, 0.0)
    assert not chain.sent


def test_jupiter_broker_needs_sol_for_fees():
    broker, chain = make_broker()
    chain.sol = 0.001
    assert broker.buy(f"{MINT}/USDC", 200, 0.5) == (0.0, 0.0)


def test_config_rejects_non_usd_solana_symbols(tmp_path):
    from cryptobot.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text("venue: solana\nsymbols: [BONK/SOL]\n")
    with pytest.raises(ValueError):
        load_config(path)
    path.write_text("venue: solana\nsymbols: [BONK/USDC, WIF/USDC]\n")
    assert load_config(path).venue == "solana"
