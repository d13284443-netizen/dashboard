"""
test_chain_and_liquidity.py — chain_loader (XLSX parsing) and
ingest.is_liquid (the gate applied once, at write time).

The liquidity gate deserves heavy coverage for a reason the README
already gives: illiquid strikes generate most false positives, so the
gate is load-bearing for alert quality, not just tidiness.
"""
import pytest

import chain_loader
import ingest
from chain_loader import to_f, mid, both_sided, detect_instrument


# --------------------------------------------------------------------
# to_f / mid / both_sided
# --------------------------------------------------------------------
class TestCoercion:
    @pytest.mark.parametrize("raw,want", [
        ("1234.5", 1234.5), ("1,234.5", 1234.5), (" 12 ", 12.0),
        (12, 12.0), (12.5, 12.5), ("-3.5", -3.5), ("0", 0.0),
    ])
    def test_parses(self, raw, want):
        assert to_f(raw) == pytest.approx(want)

    @pytest.mark.parametrize("raw", [None, "", "  ", "N/A", "--", "1.2.3", "abc", []])
    def test_rejects(self, raw):
        assert to_f(raw) is None

    def test_mid_of_both(self):
        assert mid(10, 12) == 11

    def test_mid_falls_back_to_the_single_available_side(self):
        assert mid(None, 12) == 12
        assert mid(10, None) == 10
        assert mid(None, None) is None

    def test_both_sided(self):
        assert both_sided(1, 2) is True
        assert both_sided(None, 2) is False
        assert both_sided(0, 2) is True   # zero is a quote, just a bad one


class TestInstrumentDetection:
    @pytest.mark.parametrize("symbol,want", [
        ("GCE1/Q26", "GOLD"), ("GC", "GOLD"), ("SIE2/N26", "SILVER"),
        ("ENQZ26", "NASDAQ"), ("MNQ", "NASDAQ"), ("EPU26", "SP500"),
        ("MESZ26", "SP500"), ("gce1/q26", "GOLD"),
    ])
    def test_known_roots(self, symbol, want):
        assert detect_instrument(symbol) == want

    @pytest.mark.parametrize("symbol", [None, "", "CLZ26", "Options Chain"])
    def test_unknown_returns_none(self, symbol):
        assert detect_instrument(symbol) is None

    def test_longer_roots_win(self):
        """MNQ must not be shadowed by a shorter root. Ordering by root
        length is what makes this work; the test guards the ordering."""
        assert detect_instrument("MNQZ26") == "NASDAQ"


# --------------------------------------------------------------------
# load_chain
# --------------------------------------------------------------------
class TestLoadChain:
    def test_parses_a_well_formed_file(self, chain_xlsx):
        meta, records = chain_loader.load_chain(chain_xlsx())
        assert meta["symbol"] == "GCE1/Q26"
        assert len(records) == 5
        assert [r["strike"] for r in records] == [3350, 3375, 3400, 3425, 3450]

    def test_records_are_sorted_by_strike(self, chain_xlsx):
        _m, records = chain_loader.load_chain(
            chain_xlsx(strikes=[3450, 3350, 3400]))
        assert [r["strike"] for r in records] == [3350, 3400, 3450]

    def test_derived_fields_are_populated(self, chain_xlsx):
        _m, records = chain_loader.load_chain(chain_xlsx())
        r = records[0]
        assert r["call_prem"] == pytest.approx(13.0)
        assert r["call_liquid"] is True

    def test_rows_without_a_strike_are_skipped(self, chain_xlsx):
        with pytest.raises(ValueError, match="No strike rows"):
            chain_loader.load_chain(chain_xlsx(blank_strikes=True))

    def test_short_file_raises(self, chain_xlsx):
        with pytest.raises(ValueError, match="too short"):
            chain_loader.load_chain(chain_xlsx(strikes=[], header_rows=3))

    def test_shifted_columns_are_not_detected(self, chain_xlsx):
        """KNOWN RISK, pinned deliberately.

        COL is a fixed index map with no header validation. If CQG adds
        or reorders a column, the file still parses — every field is
        just read from the wrong place. Delta becomes gamma, IV becomes
        theoretical value, and the detector runs happily on nonsense.

        The quarantine path in ingest only catches files that FAIL to
        parse, so this failure mode is invisible.

        Recommended fix: assert on row 4 (the header row) that a few
        known column names sit at their expected indices, and raise if
        not — which converts a silent corruption into a quarantined
        file and a Telegram alert.
        """
        _m, records = chain_loader.load_chain(chain_xlsx(shift_columns=2))
        assert records, "a shifted file still parses — this is the problem"
        assert records[0]["strike"] not in (3350, 3375, 3400, 3425, 3450), \
            "strike came from the wrong column, as expected"


# --------------------------------------------------------------------
# is_liquid
# --------------------------------------------------------------------
def rec(**over):
    base = {"call_bid": 12.5, "call_ask": 13.5, "call_oi": 500,
            "call_delta": 45.0, "call_impvlt": 18.2}
    base.update(over)
    return base


class TestLiquidityGate:
    def test_a_healthy_quote_passes(self):
        assert ingest.is_liquid(rec(), "call") is True

    @pytest.mark.parametrize("over", [
        {"call_bid": None}, {"call_ask": None},
        {"call_bid": 0}, {"call_ask": 0},
        {"call_bid": -1},
    ])
    def test_missing_or_nonpositive_quotes_fail(self, over):
        assert ingest.is_liquid(rec(**over), "call") is False

    def test_crossed_market_fails(self):
        assert ingest.is_liquid(rec(call_bid=14.0, call_ask=13.0), "call") is False

    def test_locked_market_passes(self):
        """bid == ask is not crossed. Unusual but not disqualifying."""
        assert ingest.is_liquid(rec(call_bid=13.0, call_ask=13.0), "call") is True

    def test_wide_spread_fails(self):
        # 50% of mid is the default ceiling
        assert ingest.is_liquid(rec(call_bid=1.0, call_ask=10.0), "call") is False

    def test_spread_exactly_at_the_limit_passes(self):
        """mid = 4.0, spread = 2.0, ratio = 0.50 — the check is `>`, so
        the boundary is inclusive. Pinned so a refactor to `>=` is a
        visible behaviour change."""
        assert ingest.is_liquid(rec(call_bid=3.0, call_ask=5.0), "call") is True

    def test_low_open_interest_fails(self):
        assert ingest.is_liquid(rec(call_oi=1), "call") is False

    @pytest.mark.parametrize("d", [1.0, 99.0, -1.0, -99.0])
    def test_delta_outside_the_band_fails(self, d):
        assert ingest.is_liquid(rec(call_delta=d), "call") is False

    @pytest.mark.parametrize("d", [-45.0, 45.0])
    def test_delta_sign_is_ignored(self, d):
        """Put deltas arrive negative; the band is on absolute value."""
        assert ingest.is_liquid(rec(call_delta=d), "call") is True

    @pytest.mark.parametrize("over", [{"call_impvlt": None}, {"call_impvlt": 0}])
    def test_missing_iv_fails(self, over):
        assert ingest.is_liquid(rec(**over), "call") is False

    def test_missing_oi_or_delta_fails_closed_by_default(self, monkeypatch):
        """DECIDED (was a documented gap). The gate now fails closed on
        missing OI or delta by default — a strike whose liquidity can't
        be confirmed is treated as illiquid, honouring the docstring's
        'any one disqualifies'. Configurable back to lenient for a feed
        that legitimately omits these fields.
        """
        # Default: missing OI or delta disqualifies.
        assert ingest.is_liquid(rec(call_oi=None), "call") is False
        assert ingest.is_liquid(rec(call_delta=None), "call") is False
        # Opt back into lenient behaviour.
        monkeypatch.setattr(ingest.config, "REQUIRE_OI", False)
        monkeypatch.setattr(ingest.config, "REQUIRE_DELTA", False)
        assert ingest.is_liquid(rec(call_oi=None), "call") is True
        assert ingest.is_liquid(rec(call_delta=None), "call") is True

    def test_a_decimal_delta_convention_would_reject_everything(self):
        """MIN_ABS_DELTA/MAX_ABS_DELTA assume CQG's 0-100 scale. If a
        future export (or a different instrument) reports 0-1 deltas,
        every strike fails the gate and the monitor goes silent without
        erroring. Worth an ingest-time sanity check on the delta range.
        """
        assert ingest.is_liquid(rec(call_delta=0.45), "call") is False

    def test_gate_is_side_specific(self):
        r = rec(put_bid=None, put_ask=None, put_oi=500,
                put_delta=-45.0, put_impvlt=18.0)
        assert ingest.is_liquid(r, "call") is True
        assert ingest.is_liquid(r, "put") is False
