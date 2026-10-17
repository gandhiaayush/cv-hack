from cvhack.contracts import Design
from cvhack.parts import from_b_record, load_parts, merge_sources
from cvhack.tooling import budgets, erc

PARTS = load_parts()


def test_b_dataset_is_loaded_with_unknowns_kept_unknown():
    p = PARTS["BQ27441DRZR-G1"]  # LCSC/JLCPCB doesn't stock it, so the real catalog can't fill it in
    assert p.ratings["part_id"] == "part:texas-instruments:bq27441drzr-g1"
    assert p.price_usd is None and p.stock is None
    assert p.pins == []  # no overlay yet, so ERC will say PIN_UNKNOWN rather than guess


def test_catalog_fills_b_unknowns_with_sourced_values():
    p = PARTS["TPS63031DSKR"]
    assert p.ratings["part_id"] == "part:texas-instruments:tps63031dskr"  # B's identity and specs kept
    assert p.price_usd is not None and p.stock and p.ratings["catalog"]["checked_at"]
    assert any(pin.type == "power_out" for pin in p.pins)  # pinout from KiCad's TPS63031DSK symbol


def test_catalog_adds_real_parts_with_pinouts():
    assert len(PARTS) >= 500
    p = PARTS["ME6211C33M5G-N"]
    assert p.category == "ldo" and p.footprint == "Package_TO_SOT_SMD:SOT-23-5"
    assert {pin.type for pin in p.pins} >= {"power_in", "power_out"}
    assert p.ratings["catalog"]["sku"].startswith("C") and p.stock > 0


def test_catalog_does_not_override_mock_demo_values():
    p = PARTS["MAX30102EFD+T"]
    assert p.price_usd == 4.10 and len(p.pins) == 6  # mock values; the real offer is kept alongside
    assert p.ratings["catalog"]["price_usd"] != p.price_usd


def test_catalog_prices_can_win():
    mock = [{"mpn": "X1", "category": "ldo", "price_usd": 1.0, "stock": 5}]
    cat = [{"mpn": "X1", "category": "ldo", "price_usd": 2.0, "stock": 7,
            "ratings": {"catalog": {"sku": "C1"}}}]
    assert merge_sources(mock, [], {}, cat)["X1"].price_usd == 1.0
    assert merge_sources(mock, [], {}, cat, catalog_prices=True)["X1"].price_usd == 2.0


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
