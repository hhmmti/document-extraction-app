"""The well-entries table: its columns and their Hive types, defined once.

One row per confirmed entry, one JSON object per line, at
`esp-design-extraction/well-entries/environment=<env>/<confirmation_id>.json` (extraction/confirmation.py builds it).
The row builder lays the two maps out from `PARAMETERS`, and the writer checks every row with `problems`; the stack
builds the table from `COLUMNS` and `PARTITION_KEYS`.  `environment` is the partition: the row carries it too, and a
JSON reader skips a key that is not a column.

Types follow the final field list: numbers are double, strings are string, and its integers (a section's order and
stages) are int.  Each parameter is an envelope struct; pump_sections' value is an array of section structs.

This file imports nothing, so the stack can load it by path without the pipeline's dependencies.
"""

ENVELOPE = ("value", "unit", "source", "as_of", "note")
SECTION = (("section_order", "int"), ("pump_model", "string"), ("stages", "int"), ("section_role", "string"))
SECTIONS = "array<struct<" + ",".join(f"{name}:{hive}" for name, hive in SECTION) + ">>"

# Each map's keys in the final field list's order, with the Hive type of the envelope's value.
PARAMETERS = {
    "pump_parameters": (
        ("pump_sections", SECTIONS),
        ("setting_depth_tvd_ft", "double"),
        ("setting_depth_md_ft", "double"),
        ("motor_type", "string"),
        ("motor_power_factor_fraction", "double"),
        ("motor_nameplate_hp", "double"),
        ("motor_efficiency_fraction", "double"),
        ("design_document_ref", "string"),
    ),
    "wellbore_parameters": (
        ("fluid_specific_gravity", "double"),
        ("oil_sg", "double"),
        ("water_sg", "double"),
        ("gas_sg", "double"),
        ("bubble_point_psia", "double"),
        ("reservoir_pressure_psia", "double"),
        ("bottomhole_temperature_f", "double"),
        ("perforation_top_depth_ft", "double"),
        ("solution_gor_scfstb", "double"),
        ("vapor_pressure_psia", "double"),
    ),
}
VALUE_TYPE = {key: hive for keys in PARAMETERS.values() for key, hive in keys}
MAP_OF = {key: field for field, keys in PARAMETERS.items() for key, _ in keys}


def envelope(value_type: str) -> str:
    return f"struct<value:{value_type}," + ",".join(f"{name}:string" for name in ENVELOPE[1:]) + ">"


def parameters(field: str) -> str:
    return "struct<" + ",".join(f"{key}:{envelope(hive)}" for key, hive in PARAMETERS[field]) + ">"


COLUMNS = (
    ("organization_id", "string"),
    ("well_id", "string"),
    ("pump_parameters", parameters("pump_parameters")),
    ("wellbore_parameters", parameters("wellbore_parameters")),
    ("run_id", "string"),
    ("document_sha256", "string"),
    ("design_date", "string"),
    ("entry_method", "string"),
    ("confirmation_id", "string"),
    ("confirmed_at", "string"),
    ("confirmed_by", "string"),
)
PARTITION_KEYS = (("environment", "string"),)
NULLABLE = {"design_date"}
INT = (-2 ** 31, 2 ** 31)          # Hive int


def fits(value, hive: str) -> bool:
    """Whether a JSON value reads back as this Hive type; null fits any."""
    if value is None:
        return True
    if hive == "double":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")
    if hive == "int":
        return isinstance(value, int) and not isinstance(value, bool) and INT[0] <= value < INT[1]
    if hive == "string":
        return isinstance(value, str)
    if hive == SECTIONS:
        return isinstance(value, list) and all(
            isinstance(s, dict) and set(s) <= {name for name, _ in SECTION} and all(fits(s.get(n), t) for n, t in SECTION)
            for s in value)
    raise ValueError(f"no rule for {hive}")


def problems(row) -> list[str]:
    """What in `row` the table cannot hold as typed; [] when every value fits its column."""
    if not isinstance(row, dict):
        return ["the row is not an object"]
    columns = COLUMNS + PARTITION_KEYS
    out = [f"{name}: not a column" for name in row if name not in dict(columns)]
    for name, hive in columns:
        value = row.get(name)
        if name in PARAMETERS:
            out += _map_problems(name, value)
        elif value is None:
            if name not in NULLABLE:
                out.append(f"{name}: missing")
        elif not fits(value, hive):
            out.append(f"{name}: not {hive}")
    return out


def _map_problems(field, mapping) -> list[str]:
    if not isinstance(mapping, dict):
        return [f"{field}: not an object"]
    types, out = dict(PARAMETERS[field]), []
    for key, env in mapping.items():
        where = f"{field}.{key}"
        if key not in types:
            out.append(f"{where}: not a field of {field}")
        elif not isinstance(env, dict):
            out.append(f"{where}: not an envelope")
        else:
            out += [f"{where}.{name}: not a field of the envelope" for name in env if name not in ENVELOPE]
            out += [f"{where}.{name}: not a string" for name in ENVELOPE[1:] if not fits(env.get(name), "string")]
            if not fits(env.get("value"), types[key]):
                out.append(f"{where}.value: not {'the section array' if types[key] == SECTIONS else types[key]}")
    return out
