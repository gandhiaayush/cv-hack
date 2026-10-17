"""Static design intent for the PPG wearable: what each subsystem must contain and what rail it sits on."""

# max_rail_v: highest voltage any part in the subsystem can see (rated parts must cover it)
# allowed: part categories that make sense here; required_any: at least one part from this set
SUBSYSTEMS = {
    "power": {
        "max_rail_v": 5.5,
        "allowed": {"ldo", "charger", "power", "capacitor", "passive", "resistor", "connector", "fuse"},
        "required_any": {"ldo", "power", "charger"},
    },
    "analog_fe": {
        "max_rail_v": 3.6,
        "allowed": {"afe", "analog", "capacitor", "passive", "resistor", "led"},
        "required_any": {"afe", "analog"},
    },
    "mcu_comms": {
        "max_rail_v": 3.6,
        "allowed": {"mcu", "capacitor", "passive", "resistor", "connector"},
        "required_any": {"mcu"},
    },
    "protection": {
        "max_rail_v": 20.0,
        "allowed": {"protection", "tvs", "fuse", "connector", "resistor", "passive", "capacitor"},
        "required_any": {"protection", "tvs", "fuse"},
    },
}
BUDGET = {"power_mw": 150.0, "cost_usd": 15.0}
DEVICE = "battery-powered Class II PPG pulse-oximeter wearable"
