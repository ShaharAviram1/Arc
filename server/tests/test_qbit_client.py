"""The qBittorrent Web API client (FR-A5).

The stub in ``tests/acquisition_helpers.QbitStub`` behaves like the real thing
where the real thing is awkward: nothing works before ``auth/login``, the
cookie can be made to expire, and ``torrents/add`` answers ``Ok.`` whether or
not it had seen the hash before.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import httpx
import pytest

from arc.config import ConfigurationError
from arc.services.acquisition.qbit import (
    API,
    COMPLETE_STATES,
    FILE_OFF,
    FILE_ON,
    SLOW_INACTIVE_SECONDS,
    SLOW_RATE_KIB,
    STOP_AT_SHARE_LIMIT,
    FileInfo,
    QbitClient,
    QbitError,
    QbitUnavailable,
    TorrentInfo,
    batch_save_path_for,
    host_path,
    save_path_for,
)
from tests.acquisition_helpers import QbitStub, acquisition_settings, torrent_blob


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


# --- Adding a batch: the .torrent itself, stopped (FR-A11) -------------------

BATCH_HASH = "e" * 40
BATCH_PATH = f"/data/downloads/batch/{BATCH_HASH}"
PACK = ["Kimetsu/NCOP.mkv", "Kimetsu/07.mkv", "Kimetsu/08.mkv"]


async def add_batch(stub: QbitStub, **overrides: object) -> str:
    async with client(stub) as qbit:
        return await qbit.add_file(
            torrent_blob(BATCH_HASH),
            save_path=BATCH_PATH,
            info_hash=BATCH_HASH,
            tags="arc,batch",
            **overrides,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("api_version", ["4", "5"])
async def test_a_file_add_is_multipart_stopped_and_moves_nothing(api_version: str) -> None:
    """The byte guarantee at the moment of the add (FR-A4's exception, D2).

    ``filePrio`` is refused while the client has no metadata, so the whole
    sequence depends on the torrent being added **as a file** and **stopped**:
    the client reports it ``stoppedDL`` at zero progress, which is the only
    state out of which "not one byte of an unwanted file" can still be true.
    """
    stub = QbitStub(api_version=api_version)

    save_path = await add_batch(stub)

    assert save_path == BATCH_PATH
    uploaded = stub.uploaded[0]
    assert uploaded["blob"] == torrent_blob(BATCH_HASH), "the .torrent went in the body"
    fields = uploaded["fields"]
    assert fields["savepath"] == BATCH_PATH
    assert fields["category"] == "arc"
    assert fields["tags"] == "arc,batch"
    assert fields["autoTMM"] == "false"
    assert fields["contentLayout"] == "Original"
    # Both, with the same value: 5.x reads the first, 4.x the second.
    assert (fields["stopped"], fields["paused"]) == ("true", "true")

    async with client(stub) as qbit:
        row = (await qbit.torrents())[0]
    assert row.state == "stoppedDL"
    assert row.progress == 0.0
    assert row.complete is False


async def test_a_second_file_add_of_the_same_torrent_succeeds() -> None:
    """A handler that crashed between the add and its commit must retry clean."""
    stub = QbitStub()

    first = await add_batch(stub)
    second = await add_batch(stub)

    assert first == second == BATCH_PATH
    assert len(stub.uploaded) == 2, "both adds were sent"
    assert [t["hash"] for t in stub.torrents] == [BATCH_HASH], "and one torrent exists"


async def test_a_file_add_the_client_refuses_and_does_not_hold_raises() -> None:
    """The 409 that means "no" rather than "already there" — hence the check."""
    stub = QbitStub()

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/add"):
            return httpx.Response(409, text="Conflict")
        return httpx.Response(200, text="[]")

    async with client(stub, transport=httpx.MockTransport(conflict)) as qbit:
        with pytest.raises(QbitError, match="did not accept the torrent file"):
            await qbit.add_file(
                torrent_blob(BATCH_HASH),
                save_path=BATCH_PATH,
                info_hash=BATCH_HASH,
                tags="arc,batch",
            )


async def test_an_unstopped_file_add_says_so_in_both_fields() -> None:
    """``stopped=False`` exists for completeness; no Arc path passes it."""
    stub = QbitStub()

    await add_batch(stub, stopped=False)

    fields = stub.uploaded[0]["fields"]
    assert (fields["stopped"], fields["paused"]) == ("false", "false")
    assert stub.torrents[0]["state"] == "downloading"


# --- Reading and writing the file selection ---------------------------------


async def test_files_are_read_with_their_indices() -> None:
    stub = QbitStub()
    stub.add_files(BATCH_HASH, PACK, size=1_400_000_000)

    async with client(stub) as qbit:
        rows = await qbit.files(BATCH_HASH)

    assert [row.index for row in rows] == [0, 1, 2]
    assert [row.name for row in rows] == PACK
    assert rows[1].size == 1_400_000_000
    assert rows[1].wanted is True, "a freshly added torrent has everything selected"


async def test_files_maps_missing_and_odd_fields_safely() -> None:
    """The client is whatever the operator pulled, and 4.3 sent no ``index``."""
    stub = QbitStub()
    stub.file_lists[BATCH_HASH] = [
        {"name": "07.mkv"},  # nothing but a name: index from the position
        {"name": "08.mkv", "index": 1, "size": -1, "priority": 1, "progress": 1.4},
        {"name": "09.mkv", "index": "2", "progress": None},
    ]

    async with client(stub) as qbit:
        rows = await qbit.files(BATCH_HASH)

    assert [row.index for row in rows] == [0, 1, 2]
    assert (rows[0].size, rows[0].priority, rows[0].progress) == (0, 0, 0.0)
    assert rows[1].size == 0, "-1 is 'not known yet', not a negative size"
    assert rows[1].progress == 1.0, "clamped: it is rendered into a progress bar"
    assert rows[1].complete is True
    assert rows[2].index == 2, "a string index falls back to the position"


@pytest.mark.parametrize(
    "row",
    [
        {"index": 2, "size": 10},  # listed, and not nameable
        {"index": 2, "name": "", "size": 10},  # named nothing at all
        ["07.mkv"],  # not even an object
    ],
)
async def test_a_listing_with_a_row_it_cannot_read_raises(row: object) -> None:
    """**Fail closed.** A row with no usable name used to be dropped.

    That is the one way this could be quietly unsafe. The dropped index would
    never be named in the ``filePrio 0`` that turns every file off — and a
    freshly added torrent has every file selected — while being absent from both
    listings the read-back compares, so it would download unseen and unrecorded.
    A pack Arc cannot enumerate is a pack Arc must not start, so the count has
    to match the payload's exactly.
    """
    stub = QbitStub()
    stub.file_lists[BATCH_HASH] = [{"name": "07.mkv", "index": 0}, row]  # type: ignore[list-item]

    async with client(stub) as qbit:
        with pytest.raises(QbitError, match="could be read"):
            await qbit.files(BATCH_HASH)


async def test_files_of_a_torrent_the_client_does_not_hold_is_empty() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        assert await qbit.files(BATCH_HASH) == []


async def test_unparseable_file_json_raises() -> None:
    stub = QbitStub()

    def garbage(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        return httpx.Response(200, text="not json")

    async with client(stub, transport=httpx.MockTransport(garbage)) as qbit:
        with pytest.raises(QbitError, match="unparseable"):
            await qbit.files(BATCH_HASH)


async def test_file_priority_sends_the_indices_joined_by_pipes() -> None:
    """qBittorrent's own shape for ``id``; the read-back must then agree."""
    stub = QbitStub()
    stub.add_files(BATCH_HASH, PACK)

    async with client(stub) as qbit:
        await qbit.file_priority(BATCH_HASH, [2, 0, 1, 1], FILE_OFF)
        await qbit.file_priority(BATCH_HASH, [1], FILE_ON)
        rows = await qbit.files(BATCH_HASH)

    assert stub.priorities == [
        {"hash": BATCH_HASH, "indices": [0, 1, 2], "priority": FILE_OFF},
        {"hash": BATCH_HASH, "indices": [1], "priority": FILE_ON},
    ], "every index off first, then only the wanted one on — in that order"
    assert [(row.index, row.priority) for row in rows] == [(0, 0), (1, 1), (2, 0)]
    assert [row.name for row in rows if row.wanted] == ["Kimetsu/07.mkv"]


async def test_setting_the_priority_of_nothing_makes_no_request() -> None:
    """An empty ``id`` is an error to the client rather than a no-op."""
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.file_priority(BATCH_HASH, [], FILE_OFF)

    assert stub.priorities == []
    assert stub.calls == []


def test_normal_priority_is_one_not_high() -> None:
    """A batch must not jump the client's queue ahead of everybody's singles."""
    assert (FILE_OFF, FILE_ON) == (0, 1)


@pytest.mark.parametrize(
    ("priority", "wanted"), [(FILE_OFF, False), (FILE_ON, True), (6, True), (7, True)]
)
def test_any_priority_above_off_means_the_client_will_fetch_it(priority: int, wanted: bool) -> None:
    """Read-back asks "is anything on?", so it is ``> 0`` and not ``== 1``.

    An operator who raised a file's priority by hand in the Web UI has still
    selected it, and the gate that refuses a batch has to say so.
    """
    row = FileInfo(index=0, name="07.mkv", size=1, priority=priority, progress=0.0)

    assert row.wanted is wanted


# --- Starting ---------------------------------------------------------------


async def test_start_sends_the_hashes_folded_and_deduplicated() -> None:
    stub = QbitStub()
    stub.add_torrent(BATCH_HASH, state="stoppedDL")

    async with client(stub) as qbit:
        await qbit.start(["E" * 40, BATCH_HASH])

    assert stub.started == [BATCH_HASH]
    assert stub.calls[-1].endswith("/torrents/start")
    assert stub.torrents[0]["state"] == "downloading"


async def test_start_falls_back_to_resume_on_a_four_x_client() -> None:
    stub = QbitStub(api_version="4")

    async with client(stub) as qbit:
        await qbit.start([BATCH_HASH])

    assert stub.started == [BATCH_HASH]
    assert stub.calls[-2:] == [f"{API}/torrents/start", f"{API}/torrents/resume"]


async def test_starting_nothing_makes_no_request() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.start([])

    assert stub.calls == []


async def test_an_unreachable_client_is_not_mistaken_for_one_without_start() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        stub.down = True
        with pytest.raises(QbitUnavailable):
            await qbit.start([BATCH_HASH])


async def test_an_expired_session_is_renewed_for_the_batch_calls_too() -> None:
    """Every new call goes through ``request``, so all four re-login on a 403."""
    stub = QbitStub()
    stub.add_files(BATCH_HASH, PACK)

    async with client(stub) as qbit:
        await qbit.version()
        for expired_call in (
            lambda: qbit.add_file(
                torrent_blob(BATCH_HASH),
                save_path=BATCH_PATH,
                info_hash=BATCH_HASH,
                tags="arc,batch",
            ),
            lambda: qbit.files(BATCH_HASH),
            lambda: qbit.file_priority(BATCH_HASH, [0], FILE_OFF),
            lambda: qbit.start([BATCH_HASH]),
        ):
            stub.expire_once = True
            await expired_call()

    assert stub.logins == 5, "the first call, then one renewal per expiry"
    assert len(stub.uploaded) == 1
    assert stub.priorities[0]["priority"] == FILE_OFF
    assert stub.started == [BATCH_HASH]


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


# --- Swarm counts: absent, unknown and zero are three different things -------


def swarm(**fields: object) -> TorrentInfo:
    row = TorrentInfo.from_json(
        {"hash": "a", "name": "n", "progress": 0.3, "state": "stalledDL", **fields}
    )
    assert row is not None
    return row


def test_the_swarm_counts_are_read_when_the_client_reports_them() -> None:
    row = swarm(num_seeds=2, num_leechs=1, num_complete=9, num_incomplete=4)

    assert (row.swarm_seeds, row.swarm_peers) == (9, 4), "the tracker's figures"
    assert (row.num_seeds, row.num_leechs) == (2, 1), "and the connected ones, for the log"
    assert row.dead_swarm is False


def test_a_client_that_reports_no_counts_says_nothing_about_the_swarm() -> None:
    """Which is not the same as saying nobody is there — the stall rule cares."""
    row = swarm()

    assert row.swarm_seeds is None and row.swarm_peers is None
    assert row.dead_swarm is False


def test_minus_one_means_not_scraped_yet_rather_than_zero() -> None:
    """qBittorrent sends -1 for a tracker figure it has not asked for yet."""
    row = swarm(num_seeds=0, num_leechs=0, num_complete=-1, num_incomplete=-1)

    assert row.swarm_seeds is None and row.swarm_peers is None
    assert row.dead_swarm is False, "unscraped is not empty"


def test_a_dead_swarm_reports_zero_and_means_it() -> None:
    row = swarm(num_seeds=0, num_leechs=0, num_complete=0, num_incomplete=0)

    assert (row.swarm_seeds, row.swarm_peers) == (0, 0)
    assert row.dead_swarm is True


def test_connecting_to_nobody_this_instant_is_not_a_dead_swarm() -> None:
    """The bug this property exists to avoid: ``num_seeds`` is 0 all the time.

    A healthy 30 %-done torrent between announces reports no connections and a
    tracker that has seen twelve seeders. Reading the first figure would delete
    it (:func:`arc.services.acquisition.jobs.stall_reason`).
    """
    row = swarm(num_seeds=0, num_leechs=0, num_complete=12, num_incomplete=3)

    assert row.dead_swarm is False


def test_time_active_is_read_and_is_not_the_age() -> None:
    row = swarm(time_active=45)

    assert row.time_active == 45


def test_a_client_that_does_not_report_time_active_says_nothing() -> None:
    assert swarm().time_active is None


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
            "queueing_enabled": True,
            "max_active_downloads": 8,
            "max_active_torrents": 12,
            "dont_count_slow_torrents": True,
            "slow_torrent_dl_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_ul_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_inactive_timer": SLOW_INACTIVE_SECONDS,
            "max_ratio_enabled": True,
            "max_ratio": 0,
            "max_ratio_act": STOP_AT_SHARE_LIMIT,
            "max_seeding_time_enabled": True,
            "max_seeding_time": 0,
        }
    ]
    assert sent == stub.preferences[0], "what it reports is what it sent"


async def test_the_policy_bounds_the_download_queue() -> None:
    """The limits Arc owns because a container restart loses them (2026-09-13)."""
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy(max_active_downloads=4, max_active_torrents=6)

    sent = stub.preferences[0]
    assert sent["queueing_enabled"] is True, "without it the limits are ignored"
    assert sent["max_active_downloads"] == 4
    assert sent["max_active_torrents"] == 6
    assert sent["dont_count_slow_torrents"] is True


def test_the_share_limit_action_is_stop_not_remove() -> None:
    """0 is Stop; 1 is Remove and 3 removes the file the transcode needs."""
    assert STOP_AT_SHARE_LIMIT == 0


async def test_the_upload_cap_is_sent_in_bytes_per_second() -> None:
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy(upload_limit_kib=64)

    assert stub.preferences[0]["up_limit"] == 65536


async def test_a_seeding_deployment_keeps_its_own_share_limits() -> None:
    """Arc does not undo a share limit an operator who seeds set by hand.

    The queue limits still go: how many downloads run at once is not a
    statement about uploading.
    """
    stub = QbitStub()

    async with client(stub) as qbit:
        await qbit.apply_policy(seeding=True, upload_limit_kib=1024)

    assert stub.preferences == [
        {
            "up_limit": 1048576,
            "queueing_enabled": True,
            "max_active_downloads": 8,
            "max_active_torrents": 12,
            "dont_count_slow_torrents": True,
            "slow_torrent_dl_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_ul_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_inactive_timer": SLOW_INACTIVE_SECONDS,
        }
    ]


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


def test_a_batch_is_filed_under_its_hash_folded() -> None:
    """``downloads/batch/<hash>``, and the hash is lowercased on the way in.

    The directory name is deliberately not a number: every id-from-path
    inference (``reject.episode_id_of``, retention's ``source_dir``) does
    ``int(parts[0])`` and must **fail** for a batch rather than attribute one
    episode's file to another.
    """
    path = batch_save_path_for("ABCDEF1234", downloads_path="/data/downloads")

    assert path == "/data/downloads/batch/abcdef1234"


def test_a_batch_path_stays_under_the_downloads_root() -> None:
    """So ``host_path`` and retention's root checks need no change at all."""
    path = batch_save_path_for("a" * 40, downloads_path="/data/downloads")

    mapped = host_path(
        f"{path}/Kimetsu/07.mkv",
        downloads_path="/data/downloads",
        host_downloads=Path("/srv/arc/downloads"),
    )

    assert mapped == Path("/srv/arc/downloads/batch") / ("a" * 40) / "Kimetsu/07.mkv"


def test_a_batch_directory_name_is_not_an_episode_id() -> None:
    """The one property the choice of directory exists for."""
    first = PurePosixPath(
        batch_save_path_for("b" * 40, downloads_path="/data/downloads")
    ).relative_to("/data/downloads")

    with pytest.raises(ValueError):
        int(first.parts[0])


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
