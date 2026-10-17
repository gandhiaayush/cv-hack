from cvhack.contracts import Design
from cvhack.parts import from_b_record, load_parts, merge_sources
from cvhack.tooling import budgets, erc

PARTS = load_parts()


def test_b_dataset_is_loaded_with_unknowns_kept_unknown():
    p = PARTS["TPS63031DSKR"]
    assert p.ratings["part_id"] == "part:texas-instruments:tps63031dskr"
    assert p.price_usd is None and p.stock is None
    assert p.pins == []  # no overlay yet, so ERC will say PIN_UNKNOWN rather than guess


def test_b_overrides_mock_but_keeps_mock_pins():
    p = PARTS["ESP32-C3-MINI-1-N4"]
    assert p.ratings["sources"] and p.price_usd is None
    assert any(pin.type == "power_in" for pin in p.pins) and p.power_mw == 85.0


def test_overlay_and_values():
    r = PARTS["CRCW060310K0FKEA"]
    assert r.value == "10k" and r.symbol_name == "R" and len(r.pins) == 2
    assert PARTS["GRM188R71C104KA01D"].value == "100nF"
    assert PARTS["GRM188R60J475KE19D"].value == "4.7uF"


def test_out_of_stock_status_maps_to_zero():
    rec = {"part_id": "x", "mpn": "X1", "category": "power", "price": {"unit_amount": 1.5},
           "stock": {"available_qty": None, "status": "out_of_stock"}}
    p = merge_sources([], [from_b_record(rec)], {})["X1"]
    assert p.stock == 0 and p.price_usd == 1.5


def test_unknown_stock_is_a_warning_not_an_error():
    d = Design.model_validate({
        "name": "t", "budgets": {"power_mw": 10, "cost_usd": 10, "area_mm2": 100}, "power_flags": ["A", "B"],
        "blocks": [{"subsystem": "x", "parts": [{"ref": "R1", "mpn": "CRCW060310K0FKEA"}],
                    "nets": [{"name": "A", "nodes": [{"ref": "R1", "pin": "1"}]},
                             {"name": "B", "nodes": [{"ref": "R1", "pin": "2"}]}]}]})
    errs, _ = budgets.check(d, PARTS)
    assert {e.code for e in errs} == {"BOM_STOCK_UNKNOWN", "BOM_PRICE_UNKNOWN"}
    assert all(e.severity == "warning" for e in errs)
    assert not any(e.code.startswith("PIN") or e.code.startswith("PART") for e in erc.check(d, PARTS))
