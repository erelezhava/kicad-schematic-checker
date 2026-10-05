"""Unit, schematic-value and dielectric normalization.

Conversions use Decimal powers of ten, so boundary values compare exactly
(10 uF == 10000 nF). Anything not recognized returns None; callers must treat
None as unresolved, never as a pass or a fail.
"""

import re
from decimal import Decimal, InvalidOperation

PREFIXES = {"p": -12, "n": -9, "u": -6, "µ": -6, "μ": -6, "m": -3, "": 0, "k": 3, "K": 3, "M": 6, "G": 9}
BASE_UNITS = {"F": "capacitance", "Ohm": "resistance", "H": "inductance", "V": "voltage",
              "A": "current", "W": "power", "Hz": "frequency"}
OHM_SPELLINGS = {"Ω": "Ohm", "Ohm": "Ohm", "Ohms": "Ohm", "ohm": "Ohm", "ohms": "Ohm"}
# Dimensionless or compound units that are recognized but never prefixed.
WHOLE_UNITS = {"%": ("percent", 0), "percent": ("percent", 0),
               "ppm": ("ppm", 0),
               "ppm/°C": ("ppm_per_degree", 0), "ppm/C": ("ppm_per_degree", 0), "ppm/K": ("ppm_per_degree", 0),
               "°C": ("temperature_celsius", 0), "degC": ("temperature_celsius", 0)}


def parse_unit(unit):
    """Return (dimension, power-of-ten exponent) for a recognized unit string, else None."""
    if not isinstance(unit, str):
        return None
    text = unit.strip()
    if text in WHOLE_UNITS:
        return WHOLE_UNITS[text]
    for spelling, canonical in OHM_SPELLINGS.items():
        if text.endswith(spelling):
            prefix = text[: -len(spelling)]
            if prefix in PREFIXES:
                return BASE_UNITS[canonical], PREFIXES[prefix]
            return None
    for base, dimension in sorted(BASE_UNITS.items(), key=lambda item: -len(item[0])):
        if base != "Ohm" and text.endswith(base):
            prefix = text[: -len(base)]
            if prefix in PREFIXES:
                return dimension, PREFIXES[prefix]
    return None


def to_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def convert(value, from_unit, to_unit):
    """Convert a number between recognized units of one dimension. Returns Decimal or None.

    Identical unit strings compare directly even when not recognized, preserving
    the original exact-unit behaviour.
    """
    number = to_decimal(value)
    if number is None:
        return None
    if isinstance(from_unit, str) and isinstance(to_unit, str) and from_unit.strip() == to_unit.strip() and from_unit.strip():
        return number
    source, target = parse_unit(from_unit), parse_unit(to_unit)
    if not source or not target or source[0] != target[0]:
        return None
    return number.scaleb(source[1] - target[1])


def unit_mismatch_reason(from_unit, to_unit):
    source, target = parse_unit(from_unit), parse_unit(to_unit)
    if not source or not target:
        return f"Unrecognized unit ({from_unit!r} vs {to_unit!r}); no conversion attempted"
    return f"Evidence unit {from_unit!r} measures {source[0]}, but the rule needs {target[0]} ({to_unit!r})"


# --- Schematic value parsing --------------------------------------------------

_UNIT_SUFFIX = r"(F|H|V|A|W|Hz|Ω|Ohms?|ohms?|R)?"
# "4k7", "2u2", "0R1", "R10": the prefix letter is the decimal point.
_INFIX = re.compile(r"(?<![\w.])(\d*)([pnuµμmkKMGR])(\d+)" + _UNIT_SUFFIX + r"(?![\w.%])")
# "2.2 uF", "10k", "100nF", "10R", "47"
_PLAIN = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*([pnuµμmkKMG]?)\s*" + _UNIT_SUFFIX + r"(?![\w.%])")


def _dimension_of_suffix(suffix):
    if not suffix:
        return None
    if suffix == "R" or suffix in OHM_SPELLINGS:
        return "resistance"
    return BASE_UNITS.get(suffix)


def schematic_quantity(text, unit):
    """Parse a KiCad field such as '2.2uF 25V X7R', '4k7' or '100n' into the rule's unit.

    Returns (Decimal, None) on success or (None, reason). Only one distinct value of
    the requested dimension may be present; otherwise the value is ambiguous.
    """
    target = parse_unit(unit)
    if not target:
        return None, f"Rule unit {unit!r} is not recognized for schematic value parsing"
    if not isinstance(text, str) or not text.strip():
        return None, "Schematic field is empty"
    dimension, exponent = target
    explicit, implicit = [], []
    spans = []
    for match in _INFIX.finditer(text):
        whole, letter, fraction, suffix = match.groups()
        suffix_dim = _dimension_of_suffix(suffix)
        if letter == "R":
            if suffix and suffix_dim != "resistance":
                continue
            power, letter_dim = 0, "resistance"
        else:
            power, letter_dim = PREFIXES[letter], suffix_dim
        number = Decimal(f"{whole or '0'}.{fraction}").scaleb(power)
        found_dim = suffix_dim or letter_dim
        (explicit if found_dim else implicit).append((found_dim, number, letter))
        spans.append(match.span())
    for match in _PLAIN.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        digits, prefix, suffix = match.groups()
        number = Decimal(digits).scaleb(PREFIXES[prefix])
        found_dim = _dimension_of_suffix(suffix)
        (explicit if found_dim else implicit).append((found_dim, number, prefix))
    matching = {n for d, n, _ in explicit if d == dimension}
    if not matching:
        allowed = IMPLICIT_PREFIXES.get(dimension, set())
        matching = {n for _, n, prefix in implicit if prefix in allowed}
    if not matching:
        return None, f"No {dimension} value could be read from {text!r}"
    if len(matching) > 1:
        return None, f"Ambiguous {dimension} values in {text!r}"
    return matching.pop().scaleb(-exponent), None


# Unit-less schematic values follow KiCad convention only where it is unambiguous:
# "100n" is a capacitance, "4k7"/"47" a resistance. A bare "1206" is not read as
# farads, and voltage/current/power/frequency must always carry their unit.
IMPLICIT_PREFIXES = {
    "capacitance": {"p", "n", "u", "µ", "μ"},
    "inductance": {"n", "u", "µ", "μ"},
    "resistance": {"", "m", "k", "K", "M", "G"},
}


# --- Dielectric names -----------------------------------------------------------

DIELECTRIC_ALIASES = {"NP0": "C0G", "NPO": "C0G", "COG": "C0G", "C0G": "C0G"}
_EIA_CODE = re.compile(r"[A-Z][0-9][A-Z]")


def dielectric(value):
    """Return the canonical EIA dielectric code, or None when not recognized."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\s_\-]", "", value).upper()
    text = DIELECTRIC_ALIASES.get(text, text)
    return text if _EIA_CODE.fullmatch(text) else None


def is_dielectric_property(rule):
    if rule.get("value_type") == "dielectric":
        return True
    return re.sub(r"[^a-z]", "", str(rule.get("property", "")).casefold()).endswith("dielectric")


def text_token(value):
    """Case- and whitespace-insensitive comparison key for generic one_of values."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def dielectric_in_text(text):
    """Find exactly one recognized dielectric code in free text such as '2.2uF 25V X7R'."""
    if not isinstance(text, str):
        return None
    whole = dielectric(text)
    if whole:
        return whole
    found = {dielectric(token) for token in re.split(r"[^A-Za-z0-9]+", text) if token}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


ENGINEERING_PREFIXES = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}


def format_quantity(value, unit):
    """Readable engineering form: Decimal('0.0000022'), 'F' -> '2.2 uF'."""
    if value == 0:
        return f"0 {unit}"
    exponent = max((e for e in ENGINEERING_PREFIXES if abs(value) >= Decimal(1).scaleb(e)), default=-12)
    scaled = value.scaleb(-exponent).normalize()
    text = format(scaled, "f")
    return f"{text} {ENGINEERING_PREFIXES[exponent]}{unit}"
