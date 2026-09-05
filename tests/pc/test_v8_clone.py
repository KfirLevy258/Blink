"""The V8 value format, walked structurally rather than scanned.

The poisoning test is the point of this module. A pattern-anchored decoder
reads the number after a field name; conversation text in the same buffer can
supply that field name, and the panel then shows a number out of somebody's
chat.
"""
import logging

from pc import v8_clone
from tests.support import v8_fixture as vfx


def _varint(n: int) -> bytes:
    """Local, so a hand-built buffer below does not borrow the writer."""
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _s(text: str) -> bytes:
    """A one-byte-per-character string, varint length."""
    return b'"' + _varint(len(text)) + text.encode("latin-1")


def test_parses_a_flat_object():
    assert v8_clone.parse(vfx.dumps({"a": 1.5, "b": True})) == {
        "a": 1.5, "b": True}


def test_parses_the_nested_window_shape():
    doc = {"rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"resetsAt": 1788628200.0, "utilization": 0.05},
        "seven_day": {"resetsAt": 1788933600.0, "utilization": 0.17}}}}
    got = v8_clone.parse(vfx.dumps(doc))
    windows = got["rate_limit_info"]["unifiedWindows"]
    assert windows["seven_day"]["resetsAt"] == 1788933600.0


def test_reads_a_zero_utilization_written_as_an_integer():
    """The minute after a reset, utilization is exactly 0 and V8 writes it as
    a Smi. A decoder that requires a double misses it."""
    got = v8_clone.parse(vfx.dumps(vfx.ints({"utilization": 0})))
    assert got["utilization"] == 0


def test_a_field_name_inside_a_string_cannot_be_mistaken_for_a_field():
    """Conversation text mentioning the field name must not become data."""
    doc = {"text": "resetsAt is the field we care about",
           "resetsAt": 1788933600.0}
    assert v8_clone.parse(vfx.dumps(doc))["resetsAt"] == 1788933600.0


def test_string_lengths_are_varints_not_single_bytes():
    long_key = "k" * 300
    assert v8_clone.parse(vfx.dumps({long_key: 1.0}))[long_key] == 1.0


def test_returns_none_for_a_buffer_it_does_not_understand():
    assert v8_clone.parse(b"\x99\x99\x99") is None


def test_never_raises_on_a_truncated_buffer():
    full = vfx.dumps({"a": 1.0})
    for cut in range(1, len(full)):
        v8_clone.parse(full[:cut])


# --- The cases where a plausible implementation is silently wrong ---


def test_a_one_byte_string_at_the_varint_boundary_does_not_desynchronise():
    """Reading one length byte works for every string under 128 characters
    and then silently shifts every field that follows."""
    for n in (127, 128, 129, 300):
        doc = {"text": "x" * n, "resetsAt": 1788933600.0}
        got = v8_clone.parse(vfx.dumps(doc))
        assert got["text"] == "x" * n
        assert got["resetsAt"] == 1788933600.0


def test_zero_is_a_reading_and_absence_is_not():
    """0 as a Smi, 0.0 as a double, and a missing field are three different
    answers. UNKNOWN is never spelled as zero."""
    smi = v8_clone.parse(vfx.dumps(vfx.ints({"utilization": 0})))
    dbl = v8_clone.parse(vfx.dumps({"utilization": 0.0}))
    absent = v8_clone.parse(vfx.dumps({"other": 1.0}))
    assert smi["utilization"] == 0 and "utilization" in smi
    assert dbl["utilization"] == 0.0 and "utilization" in dbl
    assert "utilization" not in absent


def test_reads_negative_integers():
    """Zigzag is easy to decode backwards; -1 and 1 differ by one byte."""
    doc = vfx.ints({"a": -1, "b": 1, "c": -7, "d": -300, "e": 0})
    got = v8_clone.parse(vfx.dumps(doc))
    assert got == {"a": -1, "b": 1, "c": -7, "d": -300, "e": 0}


def test_a_nested_buffer_truncated_anywhere_returns_a_value_or_none():
    full = vfx.dumps({"rate_limit_info": {"unifiedWindows": {
        "seven_day": {"resetsAt": 1788933600.0, "utilization": 0.17}}}})
    for cut in range(1, len(full)):
        got = v8_clone.parse(full[:cut])
        assert got is None or isinstance(got, dict)


def test_nesting_past_max_depth_returns_none_instead_of_recursing():
    deep = inner = {}
    for _ in range(v8_clone.MAX_DEPTH + 10):
        nxt = {}
        inner["n"] = nxt
        inner = nxt
    assert v8_clone.parse(vfx.dumps(deep)) is None


def test_nesting_within_max_depth_still_parses():
    """So the depth guard cannot pass by rejecting everything."""
    shallow = inner = {}
    for _ in range(8):
        nxt = {}
        inner["n"] = nxt
        inner = nxt
    inner["leaf"] = 1.0
    got = v8_clone.parse(vfx.dumps(shallow))
    for _ in range(8):
        got = got["n"]
    assert got["leaf"] == 1.0


def test_a_back_reference_to_an_object_that_does_not_exist():
    """No table is kept, so a back-reference yields None rather than a guess
    -- and a dangling id must not raise either."""
    payload = b"o" + _s("a") + b"^" + _varint(9999) + b"{" + _varint(1)
    assert v8_clone.parse(vfx.wrap(payload)) == {"a": None}


def test_an_unknown_tag_ends_the_parse():
    payload = b"o" + _s("a") + b"\x7f" + b"{" + _varint(1)
    assert v8_clone.parse(vfx.wrap(payload)) is None


def test_parses_arrays_and_the_other_scalar_tags():
    doc = {"list": [1.0, 2.0], "null": None, "no": False,
           "utf16": "resets → soon"}
    got = v8_clone.parse(vfx.dumps(doc))
    assert got["list"] == [1.0, 2.0]
    assert got["null"] is None
    assert got["no"] is False
    assert got["utf16"] == "resets → soon"


def test_rejects_non_buffers_and_empty_input():
    for bad in (None, "not bytes", 7, b"", b"\xff", b"\xff\x0f"):
        assert v8_clone.parse(bad) is None


def test_hostile_buffers_neither_raise_nor_hang():
    for bad in (b"\xff\x0fo" + b"\x80" * 40,
                b"\xff\x0f" + b'"' + b"\xff" * 9 + b"\x7f",
                b"\xff\x0fA" + b"\xff" * 10,
                b"\xff" * 200,
                b"\xff\x0fo" + _s("a") + b"^" + b"\x80" * 5):
        got = v8_clone.parse(bad)
        assert got is None or isinstance(got, (dict, list, str, int, float))


def test_the_module_logs_nothing_at_any_level(caplog):
    """README.md:90 promises bridge.log holds nothing secret, and this parser
    is pointed at the customer's conversations."""
    caplog.set_level(logging.NOTSET)
    v8_clone.parse(vfx.dumps({"secret": "conversation text"}))
    v8_clone.parse(b"\xff\x0f\x7fnot a value at all")
    v8_clone.parse(b"\xff\x0fo" + _s("k") + b"N\x01\x02")
    assert caplog.records == []
