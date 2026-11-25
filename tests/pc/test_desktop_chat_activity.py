"""The Claude Desktop chat light: pc/desktop_chat_activity.

Every folder is synthetic (tests/support/desktop_chat_fixtures) and every
clock is injected: poll(now) is given the time, so a 50 s hold is checked
without waiting 50 s.
"""
import builtins
import importlib
import io
import os

import pytest

from pc import desktop_chat_activity as dca
from pc import user_config
from tests.support import desktop_chat_fixtures as fx

T0 = 1_800_000_000.0


def _watch(layout, platform, **kw):
    return dca.DesktopChatActivity(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform=platform, **kw)


def _ev(name, at):
    return {"eventName": name, "eventTimestamp": at}


def _learn_churn(layout, w, sub="1", start=T0):
    """Three idle churn writes, 60 s apart, in `sub`: learned afterwards."""
    for i, t in enumerate((start + 10, start + 70, start + 130)):
        fx.grow_log(layout.idb_dir, 40)
        fx.add_blob(layout.blob_dir, sub, f"c{i}")
        assert w.poll(t) is False
    assert sub in w._churn
    return start + 130


# -- Windows ---------------------------------------------------------------

def test_windows_idle_stays_off(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    for i in range(20):
        assert w.poll(T0 + 2 * i) is False
    assert w.availability() == dca.AVAILABLE


def test_the_first_poll_is_only_a_baseline(tmp_path):
    lay = fx.make_layout(tmp_path)
    fx.grow_log(lay.idb_dir, 100)
    assert _watch(lay, "win32").poll(T0) is False


def test_windows_growth_lights_and_holds_20_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is True
    assert w.poll(T0 + 12) is True
    assert w.poll(T0 + 22) is True
    assert w.poll(T0 + 22.5) is False


def test_a_same_size_rewrite_counts_by_its_mtime(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    log = os.path.join(lay.idb_dir, "000003.log")
    st = os.stat(log)
    os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert w.poll(T0 + 2) is True


def test_log_rotation_counts_as_activity(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.rotate_log(lay.idb_dir)
    assert w.poll(T0 + 2) is True


def test_macos_hold_is_50_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 100)              # no blob: a reply
    assert w.poll(end + 100) is True
    assert w.poll(end + 149) is True
    assert w.poll(end + 150) is True
    assert w.poll(end + 151) is False


# -- the queue -------------------------------------------------------------

def test_a_silent_reply_is_bridged_by_a_pending_send(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, (T0 + 1) * 1000)])
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is True
    # Windows went 89 s without a write inside one reply.
    for t in range(4, 92, 2):
        assert w.poll(T0 + t) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, (T0 + 1) * 1000),
        _ev("claudeai.message.perceived_ttlt", (T0 + 92) * 1000)])
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 92) is True
    assert w.poll(T0 + 112) is True
    assert w.poll(T0 + 113) is False


def test_a_queue_finish_does_not_cut_an_s1_hold(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 10) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.stopped", T0 + 11)])
    assert w.poll(T0 + 15) is True
    assert w.poll(T0 + 30) is True
    assert w.poll(T0 + 31) is False


def test_a_sent_with_no_finish_ends_at_the_cap(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    assert w.poll(T0 + 1) is True
    assert w.poll(T0 + dca.PENDING_CAP_S) is True
    assert w.poll(T0 + dca.PENDING_CAP_S + 1) is False


def test_iso_timestamps_and_a_wrapped_list_are_read(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, {"version": 1, "events": [
        _ev(dca.SENT, "2027-01-15T08:00:00Z")]})
    import datetime
    sent = datetime.datetime(2027, 1, 15, 8, tzinfo=datetime.timezone.utc)
    assert w.poll(sent.timestamp() + 5) is True


def test_a_finish_after_the_send_clears_pending(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.streaming_abandoned",
                               T0 + 3)])
    assert w.poll(T0 + 5) is False


def test_other_event_names_are_ignored(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, [_ev("claudeai.something.else", T0)])
    assert w.poll(T0 + 5) is False


@pytest.mark.parametrize("events", [
    "{not json",
    "[1, 2, 3]",
    '"just a string"',
    [{"eventName": dca.SENT, "eventTimestamp": True}],
    [{"eventName": dca.SENT, "eventTimestamp": 99_999_999_999_999}],
    [{"eventName": dca.SENT, "eventTimestamp": 1_000}],
    [{"eventName": dca.SENT, "eventTimestamp": "not a date"}],
    [{"eventName": dca.SENT, "eventTimestamp": None}],
    [{"eventName": dca.SENT, "eventTimestamp": [1]}],
    [{"eventName": 7, "eventTimestamp": T0}],
    [{"eventName": dca.SENT}],
])
def test_a_malformed_queue_is_false_without_raising(tmp_path, events):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, events)
    assert w.poll(T0 + 5) is False
    assert w.availability() == dca.AVAILABLE


def test_a_missing_local_storage_folder_is_no_queue(tmp_path):
    lay = fx.make_layout(tmp_path)
    os.rmdir(lay.ls_dir)
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE


# -- macOS churn learning --------------------------------------------------

def test_macos_before_learning_log_growth_alone_does_not_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is False
    fx.rotate_log(lay.idb_dir)
    assert w.poll(T0 + 4) is False


def test_macos_before_learning_the_queue_alone_lights(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0 + 1)])
    assert w.poll(T0 + 2) is True


def test_learned_churn_pairs_off_and_unpaired_growth_lights(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 40)
    fx.add_blob(lay.blob_dir, "1", "c9")
    assert w.poll(end + 70) is False
    # The blob a poll before the log still pairs (within 3 s).
    fx.add_blob(lay.blob_dir, "1", "c10")
    assert w.poll(end + 140) is False
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 142) is False
    # Log growth with no churn blob near it: a reply.
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 200) is True


def test_occasions_closer_than_60_s_count_once(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    for i, t in enumerate((10, 30, 50, 69)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"a{i}")
        assert w.poll(T0 + t) is False
    assert "1" not in w._churn
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 100) is False            # still nothing learned
    for i, t in enumerate((75, 140)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"b{i}")
        w.poll(T0 + 100 + t)
    assert "1" in w._churn
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 400) is True


def test_a_changed_subfolder_set_forgets_learning(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.rename_subfolders(lay.blob_dir, {"1": "7", "2": "8"})
    assert w.poll(end + 10) is False
    assert w._churn == set()
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 20) is False            # back to the queue alone


def test_a_new_blob_in_a_non_churn_subfolder_lights(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.add_blob(lay.blob_dir, "2", "reply")
    assert w.poll(end + 100) is True


def test_learning_waits_while_a_send_is_pending(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    for i, t in enumerate((10, 70, 130)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"c{i}")
        assert w.poll(T0 + t) is True
    assert w._churn == set()


def test_no_blob_folder_on_macos_is_unavailable(tmp_path):
    # Without a blob listing the macOS rule cannot tell churn from a reply,
    # so it must not claim the signal. A pending send does not change that.
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, "darwin")
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.UNAVAILABLE
    assert w._log is None and w._blobs is None


def test_a_blob_folder_that_is_a_file_on_macos_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    with open(lay.blob_dir, "wb") as fh:
        fh.write(b"x")
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_blob_folder_that_goes_away_forgets_the_baseline(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 2) is False
    assert w.availability() == dca.UNAVAILABLE
    assert w._log is None and w._blobs is None
    os.rename(moved, lay.blob_dir)
    # Back again: the first poll is a fresh baseline, not a reply.
    assert w.poll(T0 + 4) is False
    assert w.availability() == dca.AVAILABLE


@pytest.mark.skipif(not (os.name == "posix" and hasattr(os, "geteuid")
                         and os.geteuid() != 0),
                    reason="needs POSIX and a non-root user")
def test_an_unreadable_blob_folder_on_macos_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    os.chmod(lay.blob_dir, 0)
    try:
        assert w.poll(T0) is False
        assert w.availability() == dca.UNAVAILABLE
    finally:
        os.chmod(lay.blob_dir, 0o755)


# -- what counts as a log ----------------------------------------------------

@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_folder_named_like_a_log_is_not_a_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    os.remove(os.path.join(lay.idb_dir, "000003.log"))
    os.mkdir(os.path.join(lay.idb_dir, "000003.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_higher_numbered_folder_does_not_hide_the_real_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    os.mkdir(os.path.join(lay.idb_dir, "000009.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    assert w._log[0] == 3
    if plat == "win32":
        # Written straight to the real log: fx.grow_log picks the highest
        # number, which here is the folder.
        with open(os.path.join(lay.idb_dir, "000003.log"), "ab") as fh:
            fh.write(os.urandom(10))
        assert w.poll(T0 + 2) is True


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make a symlink here")


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_symlink_named_like_a_log_is_not_followed(tmp_path, plat):
    # Deliberate: a leveldb never makes a symlink called N.log, so one is a
    # layout we do not recognise, even when it points at a real file.
    lay = fx.make_layout(tmp_path)
    real = os.path.join(lay.idb_dir, "000003.log")
    elsewhere = str(tmp_path / "elsewhere.bin")
    os.rename(real, elsewhere)
    _symlink_or_skip(elsewhere, real)
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_higher_numbered_symlink_does_not_hide_the_real_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    _symlink_or_skip(os.path.join(lay.idb_dir, "000003.log"),
                     os.path.join(lay.idb_dir, "000009.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    assert w._log[0] == 3


def test_a_folder_of_only_non_file_logs_is_unavailable_then_recovers(
        tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    real = os.path.join(lay.idb_dir, "000003.log")
    os.rename(real, real + ".away")
    os.mkdir(real)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE
    os.rmdir(real)
    os.rename(real + ".away", real)
    assert w.poll(T0 + 2) is False              # a fresh baseline
    assert w.availability() == dca.AVAILABLE
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 4) is True


# -- the blob folder, per platform -------------------------------------------

def test_windows_does_not_need_a_blob_folder(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 2) is True


def test_windows_with_a_blob_folder_that_is_a_file_is_available(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    with open(lay.blob_dir, "wb") as fh:
        fh.write(b"x")
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE


def test_macos_blob_folder_gone_mid_hold_drops_the_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(end + 100) is True
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    assert w.poll(end + 102) is False
    assert w.availability() == dca.UNAVAILABLE
    os.rename(moved, lay.blob_dir)
    assert w.poll(end + 104) is False           # baseline again, no hold
    assert w.availability() == dca.AVAILABLE


# -- no size threshold -------------------------------------------------------

def _scripted(tmp_path, scale):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    out = [w.poll(T0)]
    for i, t in enumerate((10, 70, 130)):
        fx.grow_log(lay.idb_dir, 40 * scale)
        fx.add_blob(lay.blob_dir, "1", f"c{i}", n_bytes=10 * scale)
        out.append(w.poll(T0 + t))
    steps = [(200, 30, None), (202, 50, None), (260, 0, None),
             (300, 20, "1"), (330, 0, "2"), (400, 0, None)]
    for t, grow, blob in steps:
        if grow:
            fx.grow_log(lay.idb_dir, grow * scale)
        if blob:
            fx.add_blob(lay.blob_dir, blob, f"s{t}", n_bytes=10 * scale)
        out.append(w.poll(T0 + t))
    return out


def test_scaling_every_write_by_100x_gives_the_same_sequence(tmp_path):
    small = _scripted(tmp_path / "small", 1)
    big = _scripted(tmp_path / "big", 100)
    assert small == big
    assert True in small and small[-1] is False


# -- availability and the setting -------------------------------------------

def test_a_missing_folder_is_unavailable(tmp_path):
    w = dca.DesktopChatActivity(
        idb_dir=str(tmp_path / "nope"), blob_dir=str(tmp_path / "nb"),
        ls_dir=str(tmp_path / "nl"), platform="win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_folder_with_no_log_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    os.remove(os.path.join(lay.idb_dir, "000003.log"))
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unsupported_platform_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "linux")
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unreadable_folder_is_false_without_raising(tmp_path,
                                                       monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")

    def refuse(path):
        raise PermissionError(13, "refused")
    monkeypatch.setattr(dca.os, "scandir", refuse)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unexpected_error_is_false_without_raising(tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca, "_newest_log", boom)
    assert w.poll(T0) is False


def test_availability_before_any_poll_polls(tmp_path):
    lay = fx.make_layout(tmp_path)
    assert _watch(lay, "win32").availability() == dca.AVAILABLE


def test_setting_off_is_off_and_false(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    user_config.put(str(home), dca.SETTING, False)
    w = _watch(lay, "win32", blink_home=str(home))
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.OFF
    assert dca.enabled(str(home)) is False
    assert dca.config_state(str(home)) is False


def test_turning_it_back_on_starts_from_a_fresh_baseline(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    w = _watch(lay, "win32", blink_home=str(home))
    w.poll(T0)
    user_config.put(str(home), dca.SETTING, False)
    w.poll(T0 + 2)
    fx.grow_log(lay.idb_dir, 100)
    user_config.put(str(home), dca.SETTING, True)
    assert w.poll(T0 + 4) is False
    assert w.availability() == dca.AVAILABLE


def test_no_config_means_on(tmp_path):
    home = tmp_path / "blink"
    home.mkdir()
    assert dca.enabled(str(home)) is True
    assert dca.config_state(str(home)) is True
    lay = fx.make_layout(tmp_path / "desk")
    w = _watch(lay, "win32", blink_home=str(home))
    w.poll(T0)
    assert w.availability() == dca.AVAILABLE


def test_an_unreadable_config_is_none_for_status_and_on_for_the_poll(
        tmp_path):
    home = tmp_path / "blink"
    home.mkdir()
    (home / user_config.FILE_NAME).write_text("{half")
    assert dca.config_state(str(home)) is None
    assert dca.enabled(str(home)) is dca.DEFAULT_ON


# -- what it never does ------------------------------------------------------

SENTINEL = b"SENTINEL-7f3a-DO-NOT-PRINT"


def test_nothing_from_the_stores_reaches_stdout_or_stderr(tmp_path, capsys):
    lay = fx.make_layout(tmp_path)
    for d in (lay.idb_dir,):
        with open(os.path.join(d, "000003.log"), "ab") as fh:
            fh.write(SENTINEL)
    fx.add_blob(lay.blob_dir, "1", "SENTINEL-name")
    fx.write_queue(lay.ls_dir, [
        {"eventName": dca.SENT, "eventTimestamp": T0,
         "payload": SENTINEL.decode()}])
    for plat in ("darwin", "win32", "linux"):
        w = _watch(lay, plat)
        for i in range(5):
            fx.grow_log(lay.idb_dir, 10)
            w.poll(T0 + 2 * i)
        w.availability()
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_the_indexeddb_and_blob_folders_are_never_opened(tmp_path,
                                                         monkeypatch):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    opened = []
    real_open = builtins.open
    real_io_open = io.open

    def rec(real):
        def opener(file, *a, **k):
            opened.append(os.fspath(file) if not isinstance(file, int)
                          else file)
            return real(file, *a, **k)
        return opener
    monkeypatch.setattr(builtins, "open", rec(real_open))
    monkeypatch.setattr(io, "open", rec(real_io_open))
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        w.poll(T0)
        monkeypatch.undo()
        fx.grow_log(lay.idb_dir, 10)
        fx.add_blob(lay.blob_dir, "2", f"n-{plat}")
        monkeypatch.setattr(builtins, "open", rec(real_open))
        monkeypatch.setattr(io, "open", rec(real_io_open))
        w.poll(T0 + 2)
    monkeypatch.undo()
    assert opened, "the recorder saw nothing; the Local Storage read opens"
    idb = os.path.realpath(lay.idb_dir)
    blob = os.path.realpath(lay.blob_dir)
    for p in opened:
        if isinstance(p, int):
            continue
        real = os.path.realpath(p)
        assert not real.startswith(idb + os.sep) and real != idb
        assert not real.startswith(blob + os.sep) and real != blob


def test_importing_the_module_does_nothing(monkeypatch):
    calls = []

    def rec(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(name)
        return f
    for name in ("scandir", "listdir", "stat"):
        monkeypatch.setattr(os, name, rec(name))
    monkeypatch.setattr(os.path, "expanduser", rec("expanduser"))
    monkeypatch.setattr(builtins, "print", rec("print"))
    importlib.reload(dca)
    monkeypatch.undo()
    assert calls == []


# -- holds at the edge, with no byte-size threshold --------------------------

@pytest.mark.parametrize("plat,hold", [("win32", 20.0), ("darwin", 50.0)])
@pytest.mark.parametrize("n_bytes", [1, 100_000])
def test_the_hold_is_exact_whatever_the_write_size(tmp_path, plat, hold,
                                                   n_bytes):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, plat)
    w.poll(T0)
    t = T0 + 10
    if plat == "darwin":
        t = _learn_churn(lay, w) + 100
    fx.grow_log(lay.idb_dir, n_bytes)
    assert w.poll(t) is True
    assert w.poll(t + hold) is True
    assert w.poll(t + hold + 0.001) is False


def test_windows_does_not_scan_the_blob_folder(tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)

    def boom(*a, **k):
        raise AssertionError("blob folder scanned on Windows")
    monkeypatch.setattr(dca, "_blob_names", boom)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.add_blob(lay.blob_dir, "2", "x")
    assert w.poll(T0 + 2) is False


def test_macos_before_learning_a_new_blob_does_not_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 10)
    fx.add_blob(lay.blob_dir, "2", "x")
    assert w.poll(T0 + 2) is False


def test_a_far_future_stamp_neither_hides_nor_cancels_a_real_send(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    far = T0 + 10 * 86400
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0), _ev(dca.SENT, far)])
    assert w.poll(T0 + 5) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.stopped", far)])
    assert w.poll(T0 + 6) is True


def test_a_log_deleted_between_listing_and_stat_is_not_unavailable(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    real_stat = os.stat
    fired = []

    def flaky(path, *a, **k):
        if str(path).endswith(".log") and lay.idb_dir in str(path) \
                and not fired:
            fired.append(1)
            raise FileNotFoundError(2, "gone")
        return real_stat(path, *a, **k)
    fx.grow_log(lay.idb_dir, 10)
    monkeypatch.setattr(dca.os, "stat", flaky)
    assert w.poll(T0 + 2) is True
    assert fired
    assert w.availability() == dca.AVAILABLE


def test_the_three_folders_come_from_one_resolve(tmp_path, monkeypatch):
    from pc import desktop_paths
    calls = []
    real = desktop_paths._resolve

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(desktop_paths, "_resolve", counting)
    dca.DesktopChatActivity(platform="darwin").poll(T0)
    assert len(calls) == 1


# -- failure handling ---------------------------------------------------------

# owner True: a method of the class. Looked up at run time, not collection:
# test_importing_the_module_does_nothing reloads the module, which makes a
# new class, and a class captured here would be the stale one.
@pytest.mark.parametrize("plat,target,owner", [
    ("win32", "_newest_log", False), ("darwin", "_newest_log", False),
    ("darwin", "_blob_names", False),
    ("win32", "_queue_events", True), ("darwin", "_queue_events", True),
    ("win32", "_pending", True), ("darwin", "_pending", True),
    ("darwin", "_blob_changes", True),
    ("win32", "_paths", True),
])
def test_an_exception_in_any_step_is_false(tmp_path, monkeypatch, plat,
                                           target, owner):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, plat)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.DesktopChatActivity if owner else dca,
                        target, boom)
    # The queue says a send is pending, so a poll that swallowed the error
    # and went on would answer True: False proves the whole poll gave up.
    assert w.poll(T0 + 1) is False


@pytest.mark.parametrize("where", [
    ("leveldb", "scan"), ("desktop_local_storage", "decode_value")])
def test_an_exception_in_a_dependency_is_false(tmp_path, monkeypatch, where):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, "win32")

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(getattr(dca, where[0]), where[1], boom)
    assert w.poll(T0 + 1) is False


def test_an_unreadable_config_does_not_turn_the_light_off(tmp_path,
                                                          monkeypatch):
    """By design (enabled()): a failed config read is the default, on."""
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, "win32", blink_home=str(home))

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.user_config, "load", boom)
    assert w.poll(T0 + 1) is True


def test_an_exception_in_path_resolution_is_false(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.desktop_paths, "indexeddb_dir", boom)
    assert dca.DesktopChatActivity(platform="win32").poll(T0) is False


def _chmod_ok():
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0


@pytest.mark.skipif(not _chmod_ok(), reason="needs POSIX and a non-root user")
@pytest.mark.parametrize("which", ["idb", "blob", "ls"])
@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_an_unreadable_directory_is_false(tmp_path, which, plat):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    target = {"idb": lay.idb_dir, "blob": lay.blob_dir,
              "ls": lay.ls_dir}[which]
    w = _watch(lay, plat)
    os.chmod(target, 0)
    # Only a readable queue can say True (a send is pending); a refused
    # leveldb or queue folder never raises and never lights by itself. A
    # refused blob folder is nothing to Windows, which never reads it, and
    # makes macOS unavailable, where it is half of the rule.
    expect = which == "blob" and plat == "win32"
    try:
        for i in range(3):
            assert w.poll(T0 + 2 * i) is expect
        if which == "idb" or (which == "blob" and plat == "darwin"):
            assert w.availability() == dca.UNAVAILABLE
    finally:
        os.chmod(target, 0o755)


@pytest.mark.skipif(not _chmod_ok(), reason="needs POSIX and a non-root user")
def test_an_unreadable_blob_subfolder_does_not_raise(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    sub = os.path.join(lay.blob_dir, "1")
    os.chmod(sub, 0)
    try:
        assert w.poll(T0) is False
        assert w.poll(T0 + 2) is False
    finally:
        os.chmod(sub, 0o755)


# -- nothing written, nothing printed ----------------------------------------

def _snapshot(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return out


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_poll_writes_nothing_to_disk(tmp_path, plat):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, plat, blink_home=str(home))
    before = _snapshot(tmp_path)
    for i in range(8):
        w.poll(T0 + 2 * i)
    w.availability()
    assert _snapshot(tmp_path) == before


def test_sentinels_never_reach_output_or_logs(tmp_path, capsys, caplog,
                                              monkeypatch):
    lay = fx.make_layout(tmp_path)
    with open(os.path.join(lay.idb_dir, "000003.log"), "ab") as fh:
        fh.write(SENTINEL)
    fx.add_blob(lay.blob_dir, "1", "SENTINEL-name")
    fx.write_queue(lay.ls_dir, SENTINEL.decode() + "{not json")
    caplog.set_level(0)

    def boom(*a, **k):
        raise RuntimeError(SENTINEL.decode())
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        for i in range(3):
            fx.grow_log(lay.idb_dir, 10)
            w.poll(T0 + 2 * i)
        monkeypatch.setattr(dca, "_newest_log", boom)
        assert w.poll(T0 + 10) is False
        monkeypatch.undo()
    out, err = capsys.readouterr()
    assert out == "" and err == ""
    assert caplog.records == []


def test_no_file_in_the_indexeddb_folders_is_opened_by_os_open(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    seen = []
    real = os.open

    def rec(path, *a, **k):
        seen.append(os.fspath(path) if not isinstance(path, int) else "")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "open", rec)
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        w.poll(T0)
        w.poll(T0 + 2)
    monkeypatch.undo()
    idb = os.path.realpath(lay.idb_dir)
    blob = os.path.realpath(lay.blob_dir)
    for p in seen:
        if not p:
            continue
        r = os.path.realpath(p)
        assert not r.startswith(idb) and not r.startswith(blob)
