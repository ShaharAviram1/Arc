"""The qBittorrent Web API client (FR-A5).

The stub in ``tests/acquisition_helpers.QbitStub`` behaves like the real thing
where the real thing is awkward: nothing works before ``auth/login``, the
cookie can be made to expire, and ``torrents/add`` answers ``Ok.`` whether or
not it had seen the hash before.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from arc.config import ConfigurationError
from arc.services.acquisition.qbit import (
    API,
    COMPLETE_STATES,
    STOP_AT_SHARE_LIMIT,
    QbitClient,
    QbitError,
    QbitUnavailable,
    TorrentInfo,
    host_path,
    save_path_for,
)
from tests.acquisition_helpers import QbitStub, acquisition_settings


def client(stub: QbitStub, **overrides: object) -> QbitClient:
    values: dict[str, object] = {
        "base_url": "http://qbit.test",
        "username": "admin",
        "password": "adminadmin",
        "category": "arc",
        "downloads_path": "/data/downloads",
        "transport": stub.transport(),
    }
    values.update(overrides)
    return QbitClient(**values)  # type: ignore[arg-type]


# --- Login ------------------------------------------------------------------


async def test_the_first_call_logs_in() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.torrents()

    assert stub.logins == 1
    assert stub.calls[0].endswith("/auth/login")


async def test_a_wrong_password_is_reported_as_unavailable() -> None:
    stub = QbitStub(password="something else")

    async with client(stub) as qbit:
        with pytest.raises(QbitUnavailable, match="credentials"):
            await qbit.login()


async def test_an_unreachable_client_is_unavailable_not_an_error() -> None:
    stub = QbitStub()
    stub.down = True

    async with client(stub) as qbit:
        with pytest.raises(QbitUnavailable, match="not reachable"):
            await qbit.torrents()


async def test_an_expired_session_is_logged_in_again_and_the_call_retried() -> None:
    stub = QbitStub()
    stub.add_torrent("abc123")

    async with client(stub) as qbit:
        await qbit.torrents()
        stub.expire_once = True
        rows = await qbit.torrents()

    assert stub.logins == 2
    assert [row.hash for row in rows] == ["abc123"]


# --- Adding -----------------------------------------------------------------


HASH = "d" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Show"


@pytest.mark.parametrize("api_version", ["4", "5"])
async def test_add_files_the_torrent_under_the_category_and_episode_directory(
    api_version: str,
) -> None:
    """Both dialects: 4.x answers ``Ok.``, 5.x a JSON summary."""
    stub = QbitStub(api_version=api_version)

    async with client(stub) as qbit:
        save_path = await qbit.add(MAGNET, episode_id=42, info_hash=HASH)

    assert save_path == "/data/downloads/42"
    added = stub.added[0]
    assert added["urls"].startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert added["category"] == "arc"
    assert added["savepath"] == "/data/downloads/42"
    assert added["tags"] == "arc,episode:42"


async def test_adding_a_torrent_the_client_already_holds_succeeds() -> None:
    """qBittorrent 5.x answers 409 for a duplicate; a retry must not fail."""
    stub = QbitStub()
    stub.add_torrent(HASH)

    async with client(stub) as qbit:
        save_path = await qbit.add(MAGNET, episode_id=42, info_hash=HASH)

    assert save_path == "/data/downloads/42"


async def test_a_409_for_a_torrent_the_client_does_not_hold_raises() -> None:
    """The same status code, the opposite meaning — hence the check."""
    stub = QbitStub()

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/add"):
            return httpx.Response(409, text="Conflict")
        return httpx.Response(200, text="[]")

    async with client(stub, transport=httpx.MockTransport(conflict)) as qbit:
        with pytest.raises(QbitError, match="refused"):
            await qbit.add(MAGNET, episode_id=1, info_hash=HASH)


async def test_a_refusal_from_a_4x_client_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/add"):
            return httpx.Response(200, text="Fails.")
        return httpx.Response(200, text="[]")

    stub = QbitStub()
    async with client(stub, transport=httpx.MockTransport(refuse)) as qbit:
        with pytest.raises(QbitError, match="refused"):
            await qbit.add(MAGNET, episode_id=1, info_hash=HASH)


# --- Info -------------------------------------------------------------------


async def test_info_is_asked_for_arcs_category_only() -> None:
    stub = QbitStub()
    stub.add_torrent("aaa", category="arc")
    stub.add_torrent("bbb", category="somebody-elses")

    async with client(stub) as qbit:
        rows = await qbit.torrents()

    assert [row.hash for row in rows] == ["aaa"]


async def test_info_fields_are_read_off_the_json() -> None:
    stub = QbitStub()
    stub.add_torrent(
        "ABCDEF",
        name="[SubsPlease] Show - 07 (1080p).mkv",
        progress=0.42,
        state="downloading",
        content_path="/data/downloads/9/[SubsPlease] Show - 07 (1080p).mkv",
        completion_on=-1,
    )

    async with client(stub) as qbit:
        row = (await qbit.torrents())[0]

    assert row.hash == "abcdef", "hashes are folded so lookups cannot miss on case"
    assert row.progress == pytest.approx(0.42)
    assert row.state == "downloading"
    assert row.complete is False
    assert row.completed_at is None


@pytest.mark.parametrize("state", sorted(COMPLETE_STATES))
def test_every_seeding_state_counts_as_complete(state: str) -> None:
    assert TorrentInfo(hash="a", name="n", progress=0.0, state=state).complete


def test_a_progress_of_one_counts_as_complete_whatever_the_state_says() -> None:
    assert TorrentInfo(hash="a", name="n", progress=1.0, state="downloading").complete


def test_a_float_a_hair_under_one_still_counts() -> None:
    assert TorrentInfo(hash="a", name="n", progress=0.9999, state="downloading").complete


def test_completion_time_is_read_when_the_client_has_one() -> None:
    row = TorrentInfo(hash="a", name="n", progress=1.0, state="uploading", completion_on=1700000000)

    assert row.completed_at is not None
    assert row.completed_at.year == 2023


# --- Delete -----------------------------------------------------------------


async def test_delete_sends_the_hashes_and_the_files_flag() -> None:
    stub = QbitStub()
    stub.add_torrent("aaa")
    stub.add_torrent("bbb")

    async with client(stub) as qbit:
        await qbit.delete(["aaa", "bbb"], delete_files=True)

    assert stub.deleted[0]["hashes"] == "aaa|bbb"
    assert stub.deleted[0]["deleteFiles"] == "true"


async def test_deleting_nothing_makes_no_request() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.delete([])

    assert stub.deleted == []


async def test_only_hashes_in_arcs_category_are_deleted() -> None:
    """The guard on the one call that destroys somebody's data."""
    stub = QbitStub()
    stub.add_torrent("aaa", category="arc")
    stub.add_torrent("bbb", category="mine")

    async with client(stub) as qbit:
        await qbit.delete(["aaa", "bbb", "ccc"])

    assert stub.deleted[0]["hashes"] == "aaa", "bbb is somebody else's, ccc is nobody's"


async def test_deleting_only_foreign_hashes_makes_no_request() -> None:
    stub = QbitStub()
    stub.add_torrent("bbb", category="mine")

    async with client(stub) as qbit:
        await qbit.delete(["bbb"])

    assert stub.deleted == []


# --- Policy: no seeding, capped upload (spec §9) -----------------------------


async def test_the_policy_stops_seeding_and_caps_the_upload() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        sent = await qbit.apply_policy(seeding=False, upload_limit_kib=512)

    assert stub.preferences == [
        {
            "up_limit": 524288,
            "max_ratio_enabled": True,
            "max_ratio": 0,
            "max_ratio_act": STOP_AT_SHARE_LIMIT,
            "max_seeding_time_enabled": True,
            "max_seeding_time": 0,
        }
    ]
    assert sent == stub.preferences[0], "what it reports is what it sent"


def test_the_share_limit_action_is_stop_not_remove() -> None:
    """0 is Stop; 1 is Remove and 3 removes the file the transcode needs."""
    assert STOP_AT_SHARE_LIMIT == 0


async def test_the_upload_cap_is_sent_in_bytes_per_second() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy(upload_limit_kib=64)

    assert stub.preferences[0]["up_limit"] == 65536


async def test_a_seeding_deployment_only_gets_the_rate_cap() -> None:
    """Arc does not undo a share limit an operator who seeds set by hand."""
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy(seeding=True, upload_limit_kib=1024)

    assert stub.preferences == [{"up_limit": 1048576}]


async def test_dht_and_pex_are_never_touched() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy()

    assert "dht" not in stub.preferences[0]
    assert "pex" not in stub.preferences[0]


# --- Stopping ---------------------------------------------------------------


async def test_stop_sends_the_hashes_folded_and_deduplicated() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.stop(["BBB", "aaa", "bbb"])

    assert stub.stopped == ["aaa", "bbb"]
    assert stub.calls[-1].endswith("/torrents/stop")


async def test_stop_falls_back_to_pause_on_a_four_x_client() -> None:
    stub = QbitStub(api_version="4")

    async with client(stub) as qbit:
        await qbit.stop(["aaa"])

    assert stub.stopped == ["aaa"]
    assert stub.calls[-2:] == [f"{API}/torrents/stop", f"{API}/torrents/pause"]


async def test_stopping_nothing_makes_no_request() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.stop([])

    assert stub.calls == []


async def test_an_unreachable_client_is_not_mistaken_for_an_old_one() -> None:
    """``down`` must raise, not be retried as if ``stop`` were unsupported."""
    stub = QbitStub()

    async with client(stub) as qbit:
        stub.down = True
        with pytest.raises(QbitUnavailable):
            await qbit.stop(["aaa"])


# --- Path mapping -----------------------------------------------------------


def test_the_save_path_is_the_episode_id() -> None:
    assert save_path_for(42, downloads_path="/data/downloads") == "/data/downloads/42"


def test_a_container_path_maps_onto_the_host_directory() -> None:
    mapped = host_path(
        "/data/downloads/42/[SubsPlease] Show - 07 (1080p).mkv",
        downloads_path="/data/downloads",
        host_downloads=Path("/Users/me/Arc/data/downloads"),
    )

    assert mapped == Path("/Users/me/Arc/data/downloads/42/[SubsPlease] Show - 07 (1080p).mkv")


def test_the_directory_itself_maps() -> None:
    mapped = host_path(
        "/data/downloads/42",
        downloads_path="/data/downloads",
        host_downloads=Path("/srv/arc/downloads"),
    )

    assert mapped == Path("/srv/arc/downloads/42")


def test_a_path_outside_the_configured_root_is_refused() -> None:
    """A save path Arc did not choose is a torrent Arc did not add."""
    with pytest.raises(QbitError, match="not under"):
        host_path(
            "/home/someone/Downloads/linux.iso",
            downloads_path="/data/downloads",
            host_downloads=Path("/srv/arc/downloads"),
        )


@pytest.mark.parametrize(
    "reported",
    [
        "/data/downloads/../../etc/passwd",
        "/data/downloads/42/../../../etc/passwd",
    ],
)
def test_a_traversal_attempt_is_refused(reported: str) -> None:
    with pytest.raises(QbitError):
        host_path(
            reported,
            downloads_path="/data/downloads",
            host_downloads=Path("/srv/arc/downloads"),
        )


# --- Building from settings -------------------------------------------------


def test_from_settings_reads_the_environment(tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path)

    built = QbitClient.from_settings(settings)

    assert built.base_url == "http://qbit.test"
    assert built.category == "arc"
    assert built.downloads_path == "/data/downloads"


def test_from_settings_says_which_variable_is_missing(tmp_path: Path) -> None:
    settings = acquisition_settings(tmp_path, qbit_pass=None)

    with pytest.raises(ConfigurationError, match="QBIT_PASS"):
        QbitClient.from_settings(settings)
