from seedbox_mcp.telegram import strip_dashes


def test_strip_dashes_em_to_comma():
    assert strip_dashes("All set — Star Wars") == "All set, Star Wars"


def test_strip_dashes_numeric_range_keeps_hyphen():
    assert strip_dashes("1995-1999") == "1995-1999"
