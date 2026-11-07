"""ERC-8021 attribution round-trip and tag-shape tests.

The tag is unbackfillable, so it gets the same treatment as the settlement
proofs: assert the exact bytes on the wire, and pair the negative case with a
known-positive fixture (an untagged calldata test alone cannot catch a broken
encoder that drops every suffix).
"""

from bot import attribution


def test_suffix_matches_erc8021_schema0_layout():
    suffix = attribution.to_data_suffix("celo_test1234")
    codes = b"celo_test1234"
    assert suffix[:len(codes)] == codes
    assert suffix[len(codes)] == len(codes)          # codesLength
    assert suffix[len(codes) + 1] == 0x00            # schemaId
    assert suffix[len(codes) + 2:] == attribution.ERC_8021_MARKER
    assert len(suffix) == len(codes) + 2 + 16


def test_marker_is_eight_8021_words():
    assert attribution.ERC_8021_MARKER.hex() == "8021" * 8


def test_tag_appends_and_preserves_calldata():
    inner = "0xa9059cbb" + "00" * 64
    tagged = attribution.tag(inner)
    assert tagged.startswith(inner)                  # execution unchanged
    assert attribution.from_calldata(tagged) == attribution.app_code().split(",")


def test_round_trip_of_known_positive_fixture():
    # A real payload shape: selector + 32-byte word, then our suffix.
    tagged = attribution.tag("0xa9059cbb" + "11" * 64, code="celo_b7k3p9da")
    assert attribution.from_calldata(tagged) == ["celo_b7k3p9da"]


def test_untagged_calldata_decodes_to_none():
    assert attribution.from_calldata("0xa9059cbb" + "11" * 64) is None


def test_multiple_codes_comma_joined():
    suffix = attribution.to_data_suffix("celo_one,celo_two")
    assert b"celo_one,celo_two" in suffix


def test_rejects_bad_codes():
    for bad in ["Celo_App", "celo app", "a" * 33, ""]:
        try:
            attribution.to_data_suffix(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid code {bad!r}")


def test_env_override_is_used(monkeypatch):
    monkeypatch.setenv("CELO_ATTRIBUTION_CODE", "celo_issued")
    assert attribution.from_calldata(attribution.tag("0x")) == ["celo_issued"]


# ------------------------------------------------- the code the entry is scored on

def test_the_assigned_code_is_on_the_wire(monkeypatch):
    """The program credits the code it issued. A tag without it credits nobody,
    and nothing on chain says so — this is the check that catches it."""
    monkeypatch.delenv("CELO_ATTRIBUTION_CODE", raising=False)
    from bot import attribution
    assert attribution.ASSIGNED_CODE in attribution.from_calldata(
        attribution.tag("0xdeadbeef"))
    assert attribution.credits_this_entry()


def test_a_stale_code_on_its_own_is_caught(monkeypatch):
    """A code from before a rename (or any other value) must fail the guard."""
    monkeypatch.setenv("CELO_ATTRIBUTION_CODE", "celo_somethingelse")
    from bot import attribution
    assert not attribution.credits_this_entry()


def test_own_code_rides_alongside_the_assigned_one(monkeypatch):
    monkeypatch.setenv("CELO_ATTRIBUTION_CODE", "celo_b97cdbaae280,celo_invoiceagent")
    from bot import attribution
    codes = attribution.from_calldata(attribution.tag("0x00"))
    assert codes[0] == "celo_b97cdbaae280"
    assert "celo_invoiceagent" in codes


def test_default_configuration_credits_this_entry():
    """The shipped configuration, with no environment, must already credit us."""
    import importlib

    from bot import attribution
    importlib.reload(attribution)
    with_no_env = attribution.DEFAULT_CODES
    assert attribution.ASSIGNED_CODE in with_no_env
    assert with_no_env[0] == attribution.ASSIGNED_CODE, "assigned code goes first"


def test_the_front_page_shows_the_code_the_chain_carries():
    """The landing page is where a judge reads the attribution claim.

    It used to hardcode our own code and omit the one the platform issued, so the
    page disagreed with every transaction. Rendering it from configuration keeps
    the visible claim and the wire in step.
    """
    from bot import attribution, serve

    html = serve.render_index()
    assert attribution.ASSIGNED_CODE in html
    assert attribution.OWN_CODE in html
    assert attribution.ASSIGNED_CODE in attribution.app_code()
