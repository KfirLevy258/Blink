from pc import theme


def test_read_builder_ignores_the_ack_key(tmp_path):
    h = str(tmp_path)
    theme.write_builder(h, 4242, ack="abc")
    assert theme.read_builder(h) == 4242
    assert theme.read_builder_ack(h) == "abc"
    theme.write_builder(h, 4242)
    assert theme.read_builder_ack(h) is None
