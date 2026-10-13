"""qBittorrent calls the progressive remux makes (M20 T6, FR-A5).

The stateful stub in :mod:`tests.acquisition_helpers` flips ``seq_dl`` and
``f_l_piece_prio`` exactly as the real client's toggles do, so "toggle only
what is off" is tested against the thing that makes it matter: a toggle sent
twice undoes itself.
"""

from __future__ import annotations

from arc.services.acquisition.qbit import QbitClient
from tests.acquisition_helpers import QbitStub
from tests.test_qbit_client import client

HASH = "c0ffee" + "0" * 34
MAGNET = f"magnet:?xt=urn:btih:{HASH}"


async def test_a_streaming_single_is_added_sequentially() -> None:
    stub = QbitStub()
    async with client(stub) as qbit:
        await qbit.add(MAGNET, episode_id=12, info_hash=HASH, sequential=True)
    assert stub.added[-1]["sequentialDownload"] == "true"
    assert stub.added[-1]["firstLastPiecePrio"] == "true"


async def test_a_plain_add_says_nothing_about_order() -> None:
    stub = QbitStub()
    async with client(stub) as qbit:
        await qbit.add(MAGNET, episode_id=12, info_hash=HASH)
    assert "sequentialDownload" not in stub.added[-1]
    assert "firstLastPiecePrio" not in stub.added[-1]


async def test_ensure_sequential_toggles_only_what_is_off_and_is_idempotent() -> None:
    stub = QbitStub()
    stub.add_torrent(HASH)
    stub.torrents[-1]["f_l_piece_prio"] = True  # one of the two already on
    async with client(stub) as qbit:
        assert await qbit.ensure_sequential(HASH) == (True, False)
        assert await qbit.ensure_sequential(HASH) == (False, False)
        info = await qbit.info(HASH)
    assert stub.toggles == [("seq_dl", HASH)]
    assert info is not None and info.seq_dl and info.f_l_piece_prio


async def test_ensure_sequential_on_a_torrent_the_client_lacks_does_nothing() -> None:
    stub = QbitStub()
    async with client(stub) as qbit:
        assert await qbit.ensure_sequential(HASH) == (False, False)
    assert stub.toggles == []


async def test_piece_states_and_properties() -> None:
    stub = QbitStub()
    stub.add_torrent(HASH)
    stub.piece_states[HASH] = [2, 2, 1, 0, "x"]  # type: ignore[list-item]
    stub.piece_sizes[HASH] = 4 << 20
    async with client(stub) as qbit:
        assert await qbit.piece_states(HASH) == [2, 2, 1, 0, 0]
        properties = await qbit.properties(HASH)
    assert properties.piece_size == 4 << 20
    assert properties.pieces_num == 5


def test_the_client_type_is_unchanged() -> None:
    assert QbitClient.add.__kwdefaults__ == {"sequential": False}
