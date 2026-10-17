from cryptobot.config import SafetyConfig
from cryptobot.safety import CAUTION, DANGER, SAFE, UNKNOWN, SafetyChecker, evaluate_contract

NOW = 1_760_000_000_000
DAY = 86_400_000
ADDR = "0x1111111111111111111111111111111111111111"


def clean_goplus(**overrides):
    data = {
        "token_symbol": "GOOD",
        "token_name": "Good Token",
        "is_open_source": "1",
        "buy_tax": "0",
        "sell_tax": "0",
        "holders": [{"address": f"0x{i:040x}", "percent": "0.02", "is_locked": 0} for i in range(1, 11)],
        "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.95", "is_locked": 0}],
    }
    data.update(overrides)
    return data


def clean_pairs(address=ADDR, **overrides):
    pair = {
        "chainId": "ethereum",
        "baseToken": {"address": address},
        "liquidity": {"usd": 2_000_000},
        "volume": {"h24": 5_000_000},
        "txns": {"h24": {"buys": 900, "sells": 850}},
        "pairCreatedAt": NOW - 400 * DAY,
        "fdv": 50_000_000,
    }
    pair.update(overrides)
    return [pair]


def evaluate(goplus=None, pairs=None, address=ADDR):
    return evaluate_contract(
        "ethereum",
        address,
        clean_goplus() if goplus is None else goplus,
        clean_pairs(address) if pairs is None else pairs,
        SafetyConfig(),
        now_ms=NOW,
    )


def test_clean_token_is_safe():
    report = evaluate()
    assert report.verdict == SAFE, report.summary()
    assert report.tradable


def test_honeypot_is_danger():
    report = evaluate(clean_goplus(is_honeypot="1"))
    assert report.verdict == DANGER
    assert any("Honeypot" in r for r in report.critical)


def test_owner_can_change_balances_is_danger():
    assert evaluate(clean_goplus(owner_change_balance="1")).verdict == DANGER


def test_unlocked_liquidity_held_by_one_wallet_is_rug_risk():
    lp = [{"address": "0x2222222222222222222222222222222222222222", "percent": "0.97", "is_locked": 0}]
    report = evaluate(clean_goplus(lp_holders=lp))
    assert report.verdict == DANGER
    assert any("rug pull" in r for r in report.critical)


def test_extreme_sell_tax_is_danger():
    assert evaluate(clean_goplus(sell_tax="0.9")).verdict == DANGER


def test_impersonated_stablecoin_is_danger():
    fake = "0x3333333333333333333333333333333333333333"
    report = evaluate(clean_goplus(token_symbol="USDT"), clean_pairs(fake), address=fake)
    assert report.verdict == DANGER
    assert any("Impersonation" in r for r in report.critical)


def test_real_usdt_is_not_flagged_as_impersonation():
    real = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    report = evaluate(clean_goplus(token_symbol="USDT"), clean_pairs(real.lower()), address=real)
    assert not any("Impersonation" in r for r in report.critical)


def test_new_illiquid_pool_is_not_safe():
    pairs = clean_pairs(liquidity={"usd": 8_000}, volume={"h24": 3_000}, pairCreatedAt=NOW - DAY // 2)
    report = evaluate(pairs=pairs)
    assert report.verdict in (CAUTION, DANGER)
    assert len(report.warnings) >= 3


def test_buys_without_sells_is_danger():
    report = evaluate(pairs=clean_pairs(txns={"h24": {"buys": 400, "sells": 2}}))
    assert report.verdict == DANGER


def test_unverified_source_is_danger():
    assert evaluate(clean_goplus(is_open_source="0")).verdict == DANGER


def test_api_failure_fails_closed():
    def broken(url, params):
        raise ConnectionError("offline")

    report = SafetyChecker(SafetyConfig(), fetch=broken).check_contract("ethereum", ADDR)
    assert report.verdict == UNKNOWN
    assert not report.tradable


def test_checker_parses_api_responses():
    def fake_fetch(url, params):
        if "gopluslabs" in url:
            return {"code": 1, "message": "OK", "result": {ADDR: clean_goplus(is_mintable="1")}}
        return {"pairs": clean_pairs(pairCreatedAt=NOW - 400 * DAY)}

    report = SafetyChecker(SafetyConfig(), fetch=fake_fetch).check_contract("ethereum", ADDR)
    assert report.tradable
    assert any("mint" in w for w in report.warnings)


def test_unsupported_chain_is_unknown():
    report = SafetyChecker(SafetyConfig(), fetch=lambda u, p: {}).check_contract("dogechain", ADDR)
    assert report.verdict == UNKNOWN


def test_market_check_blocks_thin_markets():
    checker = SafetyChecker(SafetyConfig())
    good = checker.check_market("BTC/USDT", {"quoteVolume": 5e8, "bid": 100.0, "ask": 100.01}, 1000)
    thin = checker.check_market("XYZ/USDT", {"quoteVolume": 2e4, "bid": 1.0, "ask": 1.05}, 1000)
    assert good.tradable
    assert not thin.tradable
