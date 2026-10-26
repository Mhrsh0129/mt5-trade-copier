from copier.adapters.fake_adapter import FakeAdapter
from copier.engine.symbols import auto_detect_symbol_map, normalize_symbol, resolve_symbol


def test_normalize_symbol_strips_suffixes_and_prefixes():
    assert normalize_symbol("EURUSD.m") == "EURUSD"
    assert normalize_symbol("EURUSDm") == "EURUSD"
    assert normalize_symbol("EURUSD.pro") == "EURUSD"
    assert normalize_symbol("XAUUSD-ECN") == "XAUUSD"
    assert normalize_symbol("GBPUSD_i") == "GBPUSD"
    assert normalize_symbol("#US30.cash") == "US30"


def test_explicit_map_wins_over_auto():
    res = resolve_symbol(
        "XAUUSD",
        explicit_map={"XAUUSD": "GOLD.i"},
        slave_symbols=["XAUUSD", "GOLD.i"],
    )
    assert res.slave_symbol == "GOLD.i" and res.source == "explicit"
    # Case-insensitive explicit map lookup (e.g., master emits XAUUSDm, user mapped xauusdm -> GOLD.i)
    res_ci = resolve_symbol(
        "XAUUSDm",
        explicit_map={"xauusdm": "GOLD.i"},
        slave_symbols=["GOLD.i"],
    )
    assert res_ci.slave_symbol == "GOLD.i" and res_ci.source == "explicit"


def test_auto_suffix_and_alias_detection():
    slave_syms = ["EURUSD.m", "GBPUSD.m", "GOLD.m", "WS30.cash"]
    r1 = resolve_symbol("EURUSD", slave_symbols=slave_syms)
    assert r1.slave_symbol == "EURUSD.m" and r1.source == "auto_suffix"

    r2 = resolve_symbol("XAUUSD", slave_symbols=slave_syms)
    assert r2.slave_symbol == "GOLD.m" and r2.source == "auto_alias"

    r3 = resolve_symbol("US30", slave_symbols=slave_syms)
    assert r3.slave_symbol == "WS30.cash" and r3.source == "auto_alias"

    r4 = resolve_symbol("UNKNOWNPAIR", slave_symbols=slave_syms)
    assert r4.slave_symbol is None and r4.source == "unmapped"


def test_contract_size_mismatch_warning():
    slave_specs = {
        "GOLD": FakeAdapter.spec("GOLD", digits=2, contract=10.0),
        "EURUSD.m": FakeAdapter.spec("EURUSD.m", digits=5, contract=100_000.0),
    }
    master_specs = {
        "XAUUSD": FakeAdapter.spec("XAUUSD", digits=2, contract=100.0),
        "EURUSD": FakeAdapter.spec("EURUSD", digits=5, contract=100_000.0),
    }
    batch = auto_detect_symbol_map(["XAUUSD", "EURUSD"], slave_specs, master_specs=master_specs)
    by_m = {r.master_symbol: r for r in batch}
    assert by_m["XAUUSD"].slave_symbol == "GOLD"
    assert by_m["XAUUSD"].contract_size_warning is not None
    assert "100" in by_m["XAUUSD"].contract_size_warning and "10" in by_m["XAUUSD"].contract_size_warning
    assert by_m["EURUSD"].slave_symbol == "EURUSD.m"
    assert by_m["EURUSD"].contract_size_warning is None
