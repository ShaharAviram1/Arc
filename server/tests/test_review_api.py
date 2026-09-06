"""The match-review queue over HTTP (FR-L4, FR-L6, FR-D4).

Against the real app and the real database, with only the catalogue sources
replaced by fakes — the same arrangement ``test_catalogue_api.py`` uses, for
the same reason: the routers, the dependencies and the session handling are
what is under test, and stubbing them would test the stubs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.deps import NOT_AUTHENTICATED
from arc.api.review import ALREADY_LINKED, ITEM_NOT_FOUND, NOT_IGNORED
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Anime, Episode, EpisodeState, MediaFile, ReviewState, Torrent
from arc.services.acquisition.reject import QBIT_REJECTED, WRONG_FILE
from arc.services.catalog import Breaker, CatalogService
from tests.anilist_mock import FRIEREN_ID, FakeAniList, frieren_fake
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login
from tests.mal_mock import FakeMal
from tests.mal_mock import frieren_fake as mal_frieren_fake

pytestmark = pytest.mark.pg

USER_EMAIL = "reviewer@arc.test"
USER_PASSWORD = "reviewer-password"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "downloads").mkdir()
    (tmp_path / "manual").mkdir()
    return tmp_path


@pytest.fixture
def review_settings(settings: Settings, data_dir: Path) -> Settings:
    return settings.model_copy(update={"data_dir": data_dir})


@pytest.fixture
def anilist() -> FakeAniList:
    return frieren_fake()


@pytest.fixture
def mal() -> FakeMal:
    return mal_frieren_fake()


@pytest.fixture
def review_app(
    review_settings: Settings,
    pg_engine: object,
    api_factory: SessionFactory,
    anilist: FakeAniList,
    mal: FakeMal,
) -> FastAPI:
    """The app, pointed at the test database and the fake catalogue sources."""
    from arc.main import create_app

    app = create_app(review_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    app.state.catalog = CatalogService(anilist.source(), mal.source(), Breaker(300.0))
    return app


@pytest.fixture
async def user_client(
    review_app: FastAPI, api_factory: SessionFactory
) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(review_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def admin_review_client(
    review_app: FastAPI, api_factory: SessionFactory
) -> AsyncIterator[AsyncClient]:
    from arc.models import UserRole

    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(review_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
async def anon_client(review_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(review_app) as client:
        yield client


async def seed_anime(factory: SessionFactory, **values: object) -> Anime:
    async with factory() as session:
        anime = Anime(**values)  # type: ignore[arg-type]
        session.add(anime)
        await session.commit()
        return anime


async def seed_file(
    factory: SessionFactory,
    data_dir: Path,
    *,
    name: str,
    directory: str = "manual",
    review_state: ReviewState = ReviewState.PENDING,
    confidence: float | None = None,
    candidates: list[dict[str, object]] | None = None,
    episode_id: int | None = None,
    size: int = 1024,
) -> MediaFile:
    from arc.services.library.parser import parse

    path = data_dir / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    async with factory() as session:
        media_file = MediaFile(
            path=str(path.resolve()),
            size=size,
            parsed=parse(name).as_dict(),
            review_state=review_state,
            match_confidence=confidence,
            match_candidates=candidates,
            episode_id=episode_id,
        )
        session.add(media_file)
        await session.commit()
        return media_file


FRIEREN_FILE = "[SubsPlease] Sousou no Frieren - 05 (1080p) [A1B2C3D4].mkv"
UNKNOWN_FILE = "[Group] Totally Unknown Show - 02 [720p].mkv"


class TestAuth:
    """spec §7: every API route requires a session."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/review"),
            ("get", "/api/review/summary"),
            ("get", "/api/review/1/search?q=frieren"),
            ("post", "/api/review/1/confirm"),
            ("post", "/api/review/1/ignore"),
            ("post", "/api/review/1/reopen"),
        ],
    )
    async def test_anonymous_is_401(self, anon_client: AsyncClient, method: str, path: str) -> None:
        response = await anon_client.request(
            method, path, json={"anime_id": 1, "episode_number": 1} if method == "post" else None
        )
        assert response.status_code == 401
        assert response.json()["detail"] == NOT_AUTHENTICATED

    async def test_an_ordinary_user_may_review(self, user_client: AsyncClient) -> None:
        """Not admin-only: spec §2 gives users their own review items."""
        assert (await user_client.get("/api/review")).status_code == 200

    async def test_an_admin_sees_the_same_queue(
        self, admin_review_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        body = (await admin_review_client.get("/api/review")).json()
        assert body["pending"] == 1


class TestList:
    async def test_an_empty_queue(self, user_client: AsyncClient) -> None:
        assert (await user_client.get("/api/review")).json() == {"items": [], "pending": 0}

    async def test_pending_is_the_default_filter(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        await seed_file(api_factory, data_dir, name=FRIEREN_FILE, review_state=ReviewState.AUTO)

        body = (await user_client.get("/api/review")).json()

        assert [item["name"] for item in body["items"]] == [UNKNOWN_FILE]
        assert body["pending"] == 1

    async def test_the_state_filter(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        await seed_file(api_factory, data_dir, name=FRIEREN_FILE, review_state=ReviewState.AUTO)

        body = (await user_client.get("/api/review?state=auto")).json()

        assert [item["name"] for item in body["items"]] == [FRIEREN_FILE]
        # The pending count is the *queue* depth whatever was filtered for.
        assert body["pending"] == 1

    async def test_an_unknown_state_is_422(self, user_client: AsyncClient) -> None:
        assert (await user_client.get("/api/review?state=nonsense")).status_code == 422

    async def test_the_response_never_carries_an_absolute_path(
        self,
        user_client: AsyncClient,
        api_factory: SessionFactory,
        data_dir: Path,
    ) -> None:
        """The one invariant of this router (spec §7)."""
        await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, directory="downloads/[Group] Frieren"
        )
        response = await user_client.get("/api/review")

        assert str(data_dir) not in response.text
        item = response.json()["items"][0]
        assert item["name"] == FRIEREN_FILE
        assert item["directory"] == "downloads/[Group] Frieren"
        assert not item["directory"].startswith("/")

    async def test_a_manual_drop_reports_its_directory(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=FRIEREN_FILE, directory="manual")
        assert (await user_client.get("/api/review")).json()["items"][0]["directory"] == "manual"

    async def test_the_parse_is_projected_not_dumped(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=FRIEREN_FILE)
        parsed = (await user_client.get("/api/review")).json()["items"][0]["parsed"]
        assert parsed == {
            "title": "Sousou no Frieren",
            "episode": 5,
            "season": None,
            "group": "SubsPlease",
            "resolution": "1080p",
            "kind": "episode",
        }

    async def test_candidates_are_rendered_as_show_cards(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        anime = await seed_anime(
            api_factory,
            anilist_id=FRIEREN_ID,
            title_romaji="Sousou no Frieren",
            title_english="Frieren: Beyond Journey's End",
            format="TV",
            episodes=28,
        )
        await seed_file(
            api_factory,
            data_dir,
            name=FRIEREN_FILE,
            confidence=0.71,
            candidates=[
                {
                    "anime_id": anime.id,
                    "episode_number": 5,
                    "score": 0.71,
                    "reasons": ["title 0.92"],
                    "absolute": False,
                }
            ],
        )

        item = (await user_client.get("/api/review")).json()["items"][0]

        assert item["confidence"] == 0.71
        candidate = item["candidates"][0]
        assert candidate["anime"]["title"]["preferred"] == "Frieren: Beyond Journey's End"
        assert candidate["anime"]["id"] == anime.id
        assert candidate["episode_number"] == 5
        assert candidate["reasons"] == ["title 0.92"]

    async def test_a_reason_only_entry_has_no_show(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(
            api_factory,
            data_dir,
            name=UNKNOWN_FILE,
            candidates=[{"reason": "no good candidates"}],
        )
        candidate = (await user_client.get("/api/review")).json()["items"][0]["candidates"][0]
        assert candidate["anime"] is None
        assert candidate["reason"] == "no good candidates"


class TestSummary:
    async def test_it_counts_only_pending(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        await seed_file(api_factory, data_dir, name="a.mkv", review_state=ReviewState.AUTO)
        await seed_file(api_factory, data_dir, name="b.mkv", review_state=ReviewState.IGNORED)
        await seed_file(api_factory, data_dir, name="c.mkv", review_state=ReviewState.CONFIRMED)

        assert (await user_client.get("/api/review/summary")).json() == {"pending": 1}

    async def test_it_is_zero_on_an_empty_library(self, user_client: AsyncClient) -> None:
        assert (await user_client.get("/api/review/summary")).json() == {"pending": 0}


class TestConfirm:
    async def test_it_links_and_creates_the_episode(
        self,
        user_client: AsyncClient,
        api_factory: SessionFactory,
        data_dir: Path,
    ) -> None:
        anime = await seed_anime(
            api_factory, anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28
        )
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)

        response = await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": anime.id, "episode_number": 5},
        )

        assert response.status_code == 200
        assert response.json()["review_state"] == "confirmed"

        async with api_factory() as session:
            episode = await session.scalar(
                select(Episode).where(Episode.anime_id == anime.id, Episode.number == 5)
            )
            assert episode is not None
            assert episode.state is EpisodeState.MATCHED
            # A manual drop has no known air date; inventing one would be a
            # fiction on the show page.
            assert episode.air_at is None
            stored = await session.get(MediaFile, media_file.id)
            assert stored is not None
            assert stored.episode_id == episode.id
            assert stored.review_state is ReviewState.CONFIRMED

    async def test_confirming_clears_the_matchers_confidence(
        self,
        user_client: AsyncClient,
        api_factory: SessionFactory,
        data_dir: Path,
    ) -> None:
        """A person decided, so there is no machine confidence left to report.

        Leaving 0.62 on a row somebody confirmed by hand puts a number next to
        a decision it had nothing to do with — and the review UI renders it as
        "how sure Arc is", which would be a lie. The candidate list stays: it
        is the evidence that was on screen when the choice was made.
        """
        anime = await seed_anime(
            api_factory, anilist_id=FRIEREN_ID, title_romaji="Sousou no Frieren", episodes=28
        )
        candidates: list[dict[str, object]] = [
            {"anime_id": anime.id, "episode_number": 5, "score": 0.62, "reasons": ["title 0.70"]}
        ]
        media_file = await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, confidence=0.62, candidates=candidates
        )

        body = (
            await user_client.post(
                f"/api/review/{media_file.id}/confirm",
                json={"anime_id": anime.id, "episode_number": 5},
            )
        ).json()

        assert body["confidence"] is None
        async with api_factory() as session:
            stored = await session.get(MediaFile, media_file.id)
            assert stored is not None
            assert stored.match_confidence is None
            assert stored.match_candidates == candidates

    async def test_an_ignored_file_may_be_confirmed_directly(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """An ignored file needs no detour through reopen to be confirmed.

        The 409 is on ``episode_id``, and an ignored file has none; that is
        the same rule as "only a pending or ignored file may be confirmed".
        """
        anime = await seed_anime(api_factory, anilist_id=FRIEREN_ID, title_romaji="Frieren")
        media_file = await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, review_state=ReviewState.IGNORED
        )

        response = await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": anime.id, "episode_number": 5},
        )

        assert response.status_code == 200
        assert response.json()["review_state"] == "confirmed"

    async def test_an_existing_episode_row_is_reused(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        anime = await seed_anime(api_factory, anilist_id=FRIEREN_ID, title_romaji="Frieren")
        async with api_factory() as session:
            session.add(Episode(anime_id=anime.id, number=5, state=EpisodeState.DOWNLOADED))
            await session.commit()
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)

        await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": anime.id, "episode_number": 5},
        )

        async with api_factory() as session:
            episodes = list(
                (await session.scalars(select(Episode).where(Episode.anime_id == anime.id))).all()
            )
            assert len(episodes) == 1
            assert episodes[0].state is EpisodeState.MATCHED

    async def test_a_ready_episode_is_not_downgraded(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        anime = await seed_anime(api_factory, anilist_id=FRIEREN_ID, title_romaji="Frieren")
        async with api_factory() as session:
            session.add(Episode(anime_id=anime.id, number=5, state=EpisodeState.READY))
            await session.commit()
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)

        await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": anime.id, "episode_number": 5},
        )

        async with api_factory() as session:
            episode = await session.scalar(select(Episode).where(Episode.anime_id == anime.id))
            assert episode is not None
            assert episode.state is EpisodeState.READY

    async def test_an_already_linked_file_is_409(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        anime = await seed_anime(api_factory, anilist_id=FRIEREN_ID, title_romaji="Frieren")
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)
        body = {"anime_id": anime.id, "episode_number": 5}

        assert (
            await user_client.post(f"/api/review/{media_file.id}/confirm", json=body)
        ).status_code == 200
        second = await user_client.post(f"/api/review/{media_file.id}/confirm", json=body)

        assert second.status_code == 409
        assert second.json()["detail"] == ALREADY_LINKED

    async def test_an_unknown_item_is_404(self, user_client: AsyncClient) -> None:
        response = await user_client.post(
            "/api/review/999999/confirm", json={"anime_id": 1, "episode_number": 1}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == ITEM_NOT_FOUND

    async def test_an_unknown_anime_is_404(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)
        response = await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": 987654, "episode_number": 1},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"anime_id": 1},
            {"episode_number": 1},
            {"anime_id": 1, "episode_number": 0},
            {"anime_id": 1, "episode_number": 1, "confidence": 0.99},
            {"anime_id": "one", "episode_number": 1},
        ],
    )
    async def test_a_bad_body_is_422(
        self,
        user_client: AsyncClient,
        api_factory: SessionFactory,
        data_dir: Path,
        body: dict[str, object],
    ) -> None:
        """``extra="forbid"``: an unknown field is a mistake, not a courtesy."""
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)
        response = await user_client.post(f"/api/review/{media_file.id}/confirm", json=body)
        assert response.status_code == 422


async def seed_download(
    factory: SessionFactory,
    *,
    anilist_id: int,
    number: int = 5,
    state: EpisodeState = EpisodeState.MATCHING,
    with_torrent: bool = True,
) -> Episode:
    """An episode Arc downloaded a release for, mid-match."""
    async with factory() as session:
        anime = Anime(anilist_id=anilist_id, title_romaji="Sousou no Frieren")
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=number, state=state)
        session.add(episode)
        await session.flush()
        if with_torrent:
            session.add(
                Torrent(
                    episode_id=episode.id,
                    info_hash=f"{anilist_id:040d}",
                    qbit_state="stalledUP",
                )
            )
        await session.commit()
        return episode


class TestIgnoreAndReopen:
    async def test_ignoring_a_downloaded_file_frees_the_episode(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """Otherwise the episode sits in ``matching`` for ever (spec §6)."""
        episode = await seed_download(api_factory, anilist_id=971001)
        media_file = await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, directory=f"downloads/{episode.id}"
        )

        response = await user_client.post(f"/api/review/{media_file.id}/ignore")

        assert response.status_code == 200
        async with api_factory() as session:
            after = await session.get(Episode, episode.id)
            assert after is not None
            assert after.state is EpisodeState.UNAVAILABLE
            assert after.unavailable_reason == WRONG_FILE
            torrent = await session.scalar(select(Torrent).where(Torrent.episode_id == episode.id))
            assert torrent is not None and torrent.qbit_state == QBIT_REJECTED

    async def test_ignoring_a_manually_dropped_file_touches_no_episode(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """A mislabelled file somebody dropped in says nothing about anything."""
        episode = await seed_download(api_factory, anilist_id=971002)
        media_file = await seed_file(api_factory, data_dir, name=FRIEREN_FILE)

        await user_client.post(f"/api/review/{media_file.id}/ignore")

        async with api_factory() as session:
            after = await session.get(Episode, episode.id)
            assert after is not None and after.state is EpisodeState.MATCHING

    async def test_a_file_in_a_download_directory_arc_never_chose_is_left_alone(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        episode = await seed_download(api_factory, anilist_id=971003, with_torrent=False)
        media_file = await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, directory=f"downloads/{episode.id}"
        )

        await user_client.post(f"/api/review/{media_file.id}/ignore")

        async with api_factory() as session:
            after = await session.get(Episode, episode.id)
            assert after is not None and after.state is EpisodeState.MATCHING

    async def test_an_episode_already_matched_elsewhere_is_not_disturbed(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """Another file linked to it first; this one being wrong changes nothing."""
        episode = await seed_download(api_factory, anilist_id=971004, state=EpisodeState.MATCHED)
        media_file = await seed_file(
            api_factory, data_dir, name=FRIEREN_FILE, directory=f"downloads/{episode.id}"
        )

        await user_client.post(f"/api/review/{media_file.id}/ignore")

        async with api_factory() as session:
            after = await session.get(Episode, episode.id)
            assert after is not None and after.state is EpisodeState.MATCHED

    async def test_ignore_takes_it_out_of_the_queue(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)

        response = await user_client.post(f"/api/review/{media_file.id}/ignore")

        assert response.status_code == 200
        assert response.json()["review_state"] == "ignored"
        assert (await user_client.get("/api/review")).json()["pending"] == 0

    async def test_ignore_is_idempotent(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        await user_client.post(f"/api/review/{media_file.id}/ignore")
        second = await user_client.post(f"/api/review/{media_file.id}/ignore")
        assert second.status_code == 200

    async def test_the_row_survives_being_ignored(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """Deleting it would mean the next scan re-creates and re-queues it."""
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        await user_client.post(f"/api/review/{media_file.id}/ignore")
        async with api_factory() as session:
            assert await session.get(MediaFile, media_file.id) is not None

    async def test_reopen_puts_it_back(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(
            api_factory, data_dir, name=UNKNOWN_FILE, review_state=ReviewState.IGNORED
        )

        response = await user_client.post(f"/api/review/{media_file.id}/reopen")

        assert response.status_code == 200
        assert response.json()["review_state"] == "pending"
        assert (await user_client.get("/api/review")).json()["pending"] == 1

    @pytest.mark.parametrize(
        "state", [ReviewState.PENDING, ReviewState.AUTO, ReviewState.CONFIRMED]
    )
    async def test_reopen_only_works_from_ignored(
        self,
        user_client: AsyncClient,
        api_factory: SessionFactory,
        data_dir: Path,
        state: ReviewState,
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE, review_state=state)
        response = await user_client.post(f"/api/review/{media_file.id}/reopen")
        assert response.status_code == 409
        assert response.json()["detail"] == NOT_IGNORED

    async def test_reopen_of_an_unknown_item_is_404(self, user_client: AsyncClient) -> None:
        assert (await user_client.post("/api/review/999999/reopen")).status_code == 404


class TestSearch:
    async def test_it_returns_catalogue_results(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)

        response = await user_client.get(f"/api/review/{media_file.id}/search?q=frieren")

        assert response.status_code == 200
        titles = [row["title"]["romaji"] for row in response.json()["results"]]
        assert "Sousou no Frieren" in titles

    async def test_the_results_are_cached_so_confirm_can_use_their_ids(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        """The id in a search result is the id ``confirm`` takes."""
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        results = (await user_client.get(f"/api/review/{media_file.id}/search?q=frieren")).json()[
            "results"
        ]
        chosen = next(row for row in results if row["title"]["romaji"] == "Sousou no Frieren")

        confirmed = await user_client.post(
            f"/api/review/{media_file.id}/confirm",
            json={"anime_id": chosen["id"], "episode_number": 2},
        )

        assert confirmed.status_code == 200
        async with api_factory() as session:
            anime = await session.get(Anime, chosen["id"])
            assert anime is not None
            assert anime.anilist_id == FRIEREN_ID

    async def test_a_short_query_is_422(
        self, user_client: AsyncClient, api_factory: SessionFactory, data_dir: Path
    ) -> None:
        media_file = await seed_file(api_factory, data_dir, name=UNKNOWN_FILE)
        assert (await user_client.get(f"/api/review/{media_file.id}/search?q=f")).status_code == 422

    async def test_an_unknown_item_is_404(self, user_client: AsyncClient) -> None:
        assert (await user_client.get("/api/review/999999/search?q=frieren")).status_code == 404
