"""Enum value/casing sanity: domain enums match Schwab payloads (UPPER), config
enums match the §11 YAML (lower), and both coerce from their string values."""

from trader.core.enums import (
    ConflictPolicy,
    DriftDirection,
    Mode,
    OnOvershoot,
    OrderStatus,
    OrderType,
    Side,
)


def test_domain_enums_are_uppercase() -> None:
    assert Side.BUY.value == "BUY"
    assert OrderType.LIMIT.value == "LIMIT"
    assert OrderStatus.PARTIAL_FILL.value == "PARTIAL_FILL"


def test_config_enums_are_lowercase() -> None:
    assert Mode.PAPER.value == "paper"
    assert ConflictPolicy.NET.value == "net"
    assert DriftDirection.FORWARD.value == "forward"
    assert OnOvershoot.CLAMP.value == "clamp"


def test_str_enum_equals_value() -> None:
    assert Side.BUY == "BUY"
    assert Mode.PAPER == "paper"


def test_coercion_roundtrip() -> None:
    assert Mode("paper") is Mode.PAPER
    assert Side("BUY") is Side.BUY
