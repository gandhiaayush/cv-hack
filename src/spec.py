"""Static design intent for the PPG wearable: what each subsystem must contain and what rail it sits on."""

# max_rail_v: highest voltage any part in the subsystem can see (rated parts must cover it)
# allowed: part categories that make sense here; required_any: at least one part from this set
SUBSYSTEMS = {
    "power": {
        "max_rail_v": 5.5,
        "allowed": {"ldo", "dcdc", "charger", "load_switch", "battery_protection", "fuel_gauge", "supervisor",
                    "power", "capacitor", "resistor", "inductor", "ferrite", "diode", "mosfet", "connector",
                    "fuse", "passive"},
        "required_any": {"ldo", "dcdc", "charger", "power"},
    },
    "analog_fe": {
        "max_rail_v": 3.6,
        "allowed": {"afe", "analog", "opamp", "inst_amp", "adc", "light_sensor", "current_sense", "comparator",
                    "accelerometer", "temperature_sensor", "temp_humidity_sensor", "pressure_sensor",
                    "capacitor", "resistor", "passive", "led", "inductor", "ferrite"},
        "required_any": {"afe", "analog", "light_sensor", "opamp", "inst_amp", "adc"},
    },
    "mcu_comms": {
        "max_rail_v": 3.6,
        "allowed": {"mcu", "rf_module", "crystal", "oscillator", "flash", "eeprom", "rtc", "capacitor",
                    "resistor", "passive", "inductor", "ferrite", "connector", "led", "switch"},
        "required_any": {"mcu", "rf_module"},
    },
    "protection": {
        "max_rail_v": 20.0,
        "allowed": {"protection", "tvs", "fuse", "diode", "connector", "resistor", "passive", "capacitor",
                    "mosfet", "ferrite"},
        "required_any": {"protection", "tvs", "fuse"},
    },
}
BUDGET = {"power_mw": 150.0, "cost_usd": 15.0}
DEVICE = "battery-powered Class II PPG pulse-oximeter wearable"
