"""
tests/test_notifications.py — Mixtape

Regression test for Issue #4: rating a song must notify the song's sharer.
Fails on the pre-fix code (no notification created) and passes after.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer, a separate rater, and a song shared by the sharer."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Synthwave Co", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_sharer(app, seed):
    """Rating another user's song creates a 'song_rated' notification for the sharer."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        rater_id = seed["rater"].id
        song_id = seed["song"].id

        assert get_notifications(sharer_id) == []

        rate_song(rater_id, song_id, 4)

        notes = get_notifications(sharer_id)
        assert len(notes) == 1
        assert notes[0]["type"] == "song_rated"


def test_rating_own_song_does_not_notify(app, seed):
    """Rating your own shared song should NOT create a notification."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        song_id = seed["song"].id

        rate_song(sharer_id, song_id, 5)
        assert get_notifications(sharer_id) == []
