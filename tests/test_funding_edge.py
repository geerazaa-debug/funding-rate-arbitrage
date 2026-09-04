"""Tests for fee-netted funding-edge math (pure) and dynamic symbol ranking
(against a real temp-file Database, no network)."""
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.funding_edge import (
    SymbolEdge,
    breakeven_periods,
    fee_netted_threshold,
    rank_symbols_by_fee_netted_edge,
    round_trip_fee_pct,
)
from src.storage.database import Database


class TestRoundTripFeePct:
    def test_four_fills_at_symmetric_fees(self):
        # 2 exchanges x (open + close) = 4 fills at a 0.05% (0.0005) taker
        # fee each = 0.0020 (0.20%) round trip.
        assert round_trip_fee_pct(0.0005, 0.0005) == pytest.approx(0.0020)

    def test_asymmetric_exchange_fees(self):
        assert round_trip_fee_pct(0.0005, 0.0010) == pytest.approx(0.0030)

    def test_negative_fee_raises(self):
        with pytest.raises(ValueError):
            round_trip_fee_pct(-0.0001, 0.0005)


class TestBreakevenPeriods:
    def test_computes_periods_to_recover_fee_cost(self):
        # 0.20% round-trip cost, 0.05% avg differential per period -> 4 periods
        assert breakeven_periods(0.0020, 0.0005) == pytest.approx(4.0)

    def test_zero_differential_never_breaks_even(self):
        assert breakeven_periods(0.0020, 0.0) is None

    def test_negative_round_trip_raises(self):
        with pytest.raises(ValueError):
            breakeven_periods(-0.001, 0.0005)


class TestFeeNettedThreshold:
    def test_threshold_shrinks_as_holding_period_grows(self):
        # A position held longer needs a smaller per-period differential to
        # still recover the same fixed round-trip cost.
        short_hold = fee_netted_threshold(0.0020, max_holding_periods=1)
        long_hold = fee_netted_threshold(0.0020, max_holding_periods=10)

        assert short_hold == pytest.approx(0.0020)
        assert long_hold == pytest.approx(0.0002)
        assert long_hold < short_hold

    def test_zero_or_negative_holding_periods_raises(self):
        with pytest.raises(ValueError):
            fee_netted_threshold(0.0020, max_holding_periods=0)


class FakeDb:
    """Stands in for Database.get_observation_stats() with canned per-symbol
    stats, so ranking logic can be tested without SQLite."""

    def __init__(self, stats_by_symbol: dict):
        self._stats = stats_by_symbol

    def get_observation_stats(self, symbol, threshold):
        return self._stats[symbol]


class TestRankSymbolsByFeeNettedEdge:
    def test_ranks_best_edge_first(self):
        db = FakeDb({
            "GOODCOIN": {"samples": 100, "avg_abs_diff": 0.10, "max_abs_diff": 0.30,
                        "tradeable_pct": 40.0},
            "BADCOIN": {"samples": 100, "avg_abs_diff": 0.01, "max_abs_diff": 0.02,
                       "tradeable_pct": 0.5},
        })
        symbols = [{"symbol": "GOODCOIN", "samples": 100}, {"symbol": "BADCOIN", "samples": 100}]

        rankings = rank_symbols_by_fee_netted_edge(
            db, symbols, bybit_taker_fee_pct=0.0005, binance_taker_fee_pct=0.0005,
            max_holding_periods=3, min_samples=30,
        )

        assert [r.symbol for r in rankings] == ["GOODCOIN", "BADCOIN"]
        assert rankings[0].breakeven_periods < rankings[1].breakeven_periods

    def test_insufficient_samples_flagged_in_verdict(self):
        db = FakeDb({
            "THIN": {"samples": 5, "avg_abs_diff": 0.5, "max_abs_diff": 0.5,
                    "tradeable_pct": 100.0},
        })
        symbols = [{"symbol": "THIN", "samples": 5}]

        rankings = rank_symbols_by_fee_netted_edge(
            db, symbols, bybit_taker_fee_pct=0.0005, binance_taker_fee_pct=0.0005,
            max_holding_periods=3, min_samples=30,
        )

        assert "insufficient data" in rankings[0].verdict

    def test_zero_differential_symbol_has_no_breakeven_and_ranks_last(self):
        db = FakeDb({
            "DEAD": {"samples": 100, "avg_abs_diff": 0.0, "max_abs_diff": 0.0,
                    "tradeable_pct": 0.0},
            "ALIVE": {"samples": 100, "avg_abs_diff": 0.02, "max_abs_diff": 0.05,
                     "tradeable_pct": 10.0},
        })
        symbols = [{"symbol": "DEAD", "samples": 100}, {"symbol": "ALIVE", "samples": 100}]

        rankings = rank_symbols_by_fee_netted_edge(
            db, symbols, bybit_taker_fee_pct=0.0005, binance_taker_fee_pct=0.0005,
            max_holding_periods=3, min_samples=30,
        )

        dead = next(r for r in rankings if r.symbol == "DEAD")
        assert dead.breakeven_periods is None
        assert rankings[-1].symbol == "DEAD"  # sorts last, not crashes/first


class TestDatabaseIntegration:
    """Real SQLite, no mocks - catches wiring bugs the FakeDb tests above
    can't (e.g. the differential_8h normalisation fix)."""

    @pytest.fixture
    def db(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(str(Path(tmp) / "test.db"))
            database.connect()
            yield database
            database.disconnect()

    def _insert(self, db, symbol, differential, differential_8h,
                observed_at="2026-01-01T00:00:00+00:00"):
        db.insert_observations_bulk([
            (observed_at, symbol, 0.0001, 0.0001 + differential, differential,
             8.0, 8.0, differential_8h)
        ])

    def test_get_observation_stats_uses_normalised_differential(self, db):
        """A 4h-interval symbol's raw differential is NOT directly comparable
        to an 8h symbol's - differential_8h is what get_observation_stats
        must use, not raw differential (this was a real bug: it previously
        always used raw differential)."""
        # Raw differential of 0.001 on a 4h interval normalises to 0.002 on
        # an 8h basis (funding accrues twice as often).
        db.insert_observations_bulk([
            ("2026-01-01T00:00:00+00:00", "FASTCOIN", 0.0001, 0.0011, 0.001,
             4.0, 4.0, 0.002),
        ])

        stats = db.get_observation_stats("FASTCOIN", threshold=0.0015)
        assert stats["avg_abs_diff"] == pytest.approx(0.002)
        assert stats["tradeable_samples"] == 1  # 0.002 >= 0.0015 threshold

    def test_get_observation_stats_falls_back_to_raw_for_legacy_rows(self, db):
        """Rows from before the differential_8h migration have NULL there -
        must fall back to raw differential, not treat it as zero."""
        cursor = db.connection.cursor()
        cursor.execute(
            """INSERT INTO funding_observations
               (observed_at, symbol, bybit_rate, binance_rate, differential)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-01-01T00:00:00+00:00", "LEGACY", 0.0001, 0.0006, 0.0005),
        )
        db.connection.commit()

        stats = db.get_observation_stats("LEGACY", threshold=0.0001)
        assert stats["avg_abs_diff"] == pytest.approx(0.0005)

    def test_get_distinct_symbols_returns_full_collected_universe(self, db):
        self._insert(db, "BTCUSDT", 0.0001, 0.0001)
        self._insert(db, "ALTCOIN1", 0.0002, 0.0002)
        self._insert(db, "ALTCOIN2", 0.0003, 0.0003)

        symbols = db.get_distinct_symbols()
        assert {s["symbol"] for s in symbols} == {"BTCUSDT", "ALTCOIN1", "ALTCOIN2"}

    def test_get_distinct_symbols_respects_min_samples(self, db):
        self._insert(db, "ONESAMPLE", 0.0001, 0.0001)
        for _ in range(5):
            self._insert(db, "FIVESAMPLES", 0.0001, 0.0001)

        symbols = db.get_distinct_symbols(min_samples=3)
        assert {s["symbol"] for s in symbols} == {"FIVESAMPLES"}

    def test_end_to_end_ranking_against_real_database(self, db):
        """The full path: insert real rows, discover symbols, rank them -
        no mocks anywhere in this test."""
        for _ in range(40):
            self._insert(db, "WIDEDIFF", 0.003, 0.003)
        for _ in range(40):
            self._insert(db, "NARROWDIFF", 0.0001, 0.0001)

        symbols = db.get_distinct_symbols(min_samples=30)
        rankings = rank_symbols_by_fee_netted_edge(
            db, symbols, bybit_taker_fee_pct=0.0005, binance_taker_fee_pct=0.0005,
            max_holding_periods=3, min_samples=30,
        )

        assert rankings[0].symbol == "WIDEDIFF"
        assert rankings[0].fee_netted_tradeable_pct == pytest.approx(100.0)
        assert rankings[-1].symbol == "NARROWDIFF"
        assert rankings[-1].fee_netted_tradeable_pct == pytest.approx(0.0)
