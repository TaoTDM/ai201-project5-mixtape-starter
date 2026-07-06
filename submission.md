# Project 5 — Mixtape Bug Hunt: Submission

**Branch:** `bugfix/mixtape`

---

## AI Usage

I used an AI assistant (Claude) primarily for **codebase navigation and diagnosis verification**, not for generating fixes. Concretely:

- **Orientation.** I asked the AI to summarize each `services/*.py` file and to trace two call chains (rate-a-song and view-a-playlist) from route → service → model. This produced the first draft of my codebase map, which I verified by reading each file myself.
- **Data-flow tracing.** I asked it to trace "how adding a song to a playlist produces a notification" so I had a known-good reference to compare against the broken rating path (Issue #4) — that comparison made Issue #4's architectural nature obvious.
- **Targeted concept checks.** For Issue #1 I asked "what's the difference between `datetime.weekday()` and `isoweekday()`?" *after* narrowing the bug to the Sunday guard, then verified by running `update_listening_streak` with a Sunday datetime.
- **Where the AI was wrong / incomplete.** The AI claimed Issue #3 (search duplicates) would show visibly duplicated rows and that the fix was "add `.distinct()`." When I reproduced it, `search_songs()` returned no duplicates and `tests/test_search.py` already passed. Reading the code myself revealed the truth: SQLAlchemy's legacy `db.session.query(Song)` de-duplicates entities by primary key, so the pointless `outerjoin(song_tags)` is a *latent* duplication bug (the raw join yields 3 rows for a 3-tag song) masked by the ORM. I only caught that by running the query at the SQL level.

Every fix below was written by me after reproducing the bug; the AI was a navigation/rubber-duck tool, not the author of the diagnoses.

---

## Codebase Map

Mixtape is a Flask JSON API using the **application-factory** pattern with a clean **route → service → model** separation. All five bugs live in the service layer.

### Main files and their roles

- **`app.py`** — App factory. `create_app(config=None)` configures SQLite, registers four blueprints under `/songs`, `/playlists`, `/users`, `/feed`, and runs `db.create_all()`. `db = SQLAlchemy()` is a module-level singleton.
- **`models.py`** — 6 models (`User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`) plus 3 association tables: `friendships` (symmetric), `song_tags`, and `playlist_entries`. `playlist_entries` carries a **`position`** column, so playlist songs have explicit order. `User.friends` is a self-referential dynamic relationship. Every model has `to_dict()`.
- **`routes/`** — Thin controllers: parse request, call one service, `jsonify`. `songs.py` (search/get/rate/listen), `playlists.py` (create/get/list-songs/add-song), `users.py` (profile/streak/notifications/read), `feed.py` (listening-now/activity).
- **`services/`** — All logic and all five bugs: `streak_service.py` [#1], `feed_service.py` [#2], `search_service.py` [#3], `notification_service.py` [#4, plus the correct `add_to_playlist` notification pattern], `playlist_service.py` [#5].
- **`seed_data.py`** — Recreates all tables; inserts 5 users, 10 tags, 13 songs grouped by tag count (the 3+-tag set targets Issue #3), friendships, recent (~10–20 min) and older (2 h–14 day) listening events, per-user `last_listened_at`, 3 playlists, and one sample `song_added_to_playlist` notification.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`, and `test_notifications.py` (my regression test); each defines a local in-memory-SQLite `app` fixture (no `conftest.py`) and tests service functions directly.

### Data-flow trace — "a friend rates my shared song" (the Issue #4 path)

1. `POST /songs/<song_id>/rate` with `{user_id, score}`.
2. `routes/songs.py` `rate()` parses/validates and calls `notification_service.rate_song(...)`.
3. `rate_song()` validates score (1–5), loads song + rater, then **upserts** a `Rating` (unique on `user_id`+`song_id`), commits, returns it.
4. Route returns the rating as JSON.

The sibling `add_to_playlist()` does one extra step `rate_song()` omits: after commit, if the actor isn't the sharer, it calls `create_notification(user_id=song.shared_by, ...)`. That asymmetry is exactly Issue #4.

### Patterns I noticed

- Routes never touch the DB directly — all logic is in services, so tracing a bug is: route → one service call → that function.
- Notifications are created as a **side effect inside the triggering action**, so a missing notification is a missing block, not a broken function.
- Association tables carry data (`playlist_entries.position`), queried explicitly with `.join(...)`.

---

## Root Cause Analyses

_(Each code fix is its own `fix:` commit on `bugfix/mixtape`.)_

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** I called `update_listening_streak(user, now)` with `last_listened_at` on a Saturday and `now` set to the following Sunday (`datetime(2026, 7, 5, ...)`, `.weekday()` == 6). The streak reset to `1` instead of incrementing. (Today, 2026-07-05, is a Sunday, so `POST /songs/<id>/listen` reproduces it live too.) `tests/test_streaks.py::test_streak_increments_on_sunday` also fails, asserting `2 == 1`.

**How I found the root cause.** README maps this to `streak_service.py`. Reading `update_listening_streak` top-down, the consecutive-day branch is `elif days_since_last == 1 and today.weekday() != 6:`. A streak rule inspecting the day of the week is nonsensical — that was the moment of certainty.

**The root cause.** `datetime.weekday()` returns `6` for Sunday. The `and today.weekday() != 6` guard makes the `elif` false on Sundays, so control falls to `else` (`listening_streak = 1`). The streak resets every Sunday even after a consecutive listen. (Classic `weekday()` [Sun=6] vs `isoweekday()` [Sun=7] confusion — but the correct logic needs no weekday check at all.)

**My fix and side-effect check.** Removed the clause so the branch is `elif days_since_last == 1:`. Re-ran `tests/test_streaks.py` — all pass (new-user=1, same-day no-change, consecutive increment, multi-day reset), confirming the other branches are intact.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Viewing the feed as **darius** (`get_friends_listening_now(darius.id)`) returned simone (~15 min ago) *and* nova, whose most recent listen was **~2 hours ago**. A "Listening Now" feed surfacing someone from 2 hours ago is the reported bug.

**How I found the root cause.** README maps this to `feed_service.py`. `get_friends_listening_now` builds `cutoff = now - RECENT_THRESHOLD` and filters `listened_at >= cutoff`. The query/dedup are correct; the only value deciding "recent" is `RECENT_THRESHOLD = timedelta(hours=24)`.

**The root cause.** A 24-hour window for a live "listening now" feature. The boundary logic worked — the constant was simply set far too wide, so anyone active in the last day counts as "now."

**My fix and side-effect check.** Changed it to `timedelta(minutes=30)`. As darius, the feed now returns only simone (~15 min); nova (2 h) drops out while genuinely-recent listeners (seed events at 10/15/20 min) remain. `get_activity_feed` is unaffected — it ignores recency and doesn't use the constant.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it.** Subtle: `search_songs("Crown")` returns exactly **one** row and `tests/test_search.py` already passes — the user-facing duplicate does NOT currently manifest. But running the underlying query at the SQL level, the `outerjoin(song_tags)` produces **3 rows** for the 3-tag song "Crown Heights Anthem". The duplication is real but latent.

**How I found the root cause.** README maps this to `search_service.py`. `search_songs` does `query(Song).outerjoin(song_tags, ...).filter(...).all()`. The WHERE clause only touches title/artist — tags are never used — so the join only multiplies rows by tag count. No duplicates surface because SQLAlchemy's **legacy `Query`, selecting a single mapped entity, de-duplicates by primary key**. I confirmed by counting rows through the same join (`.count()` → 3), which bypasses that de-dup.

**The root cause.** An unnecessary `outerjoin(song_tags)` with no `.distinct()`. A song with N tags maps to N joined rows; the code avoids returning duplicates only by accidentally relying on legacy-`Query` entity de-dup. Switching to 2.0-style `select()`, `.count()`, or selecting columns would immediately surface real duplicates.

**My fix and side-effect check.** Removed the `.outerjoin(song_tags, ...)` line — the filter never referenced tags, and each song's tags still load via the `Song.tags` relationship in `to_dict()`. Search now returns one row per match by construction. Re-ran `tests/test_search.py`: all pass, and tag data still appears in each result.

### Issue #4 — Notified when a friend adds my song to a playlist, but not when they rate it

**How I reproduced it.** `rate_song(kenji.id, song_shared_by_simone.id, 5)` then `get_notifications(simone.id)` returned an empty list before and after. By contrast, `add_to_playlist(...)` on the same song *does* create a notification — confirming the asymmetry.

**How I found the root cause.** README maps this to `notification_service.py`, which holds both `add_to_playlist` and `rate_song`. Side by side: `add_to_playlist` ends with `if song.shared_by != added_by_user_id: create_notification(...)`; `rate_song` upserts the rating, commits, returns — with no `create_notification` anywhere. Architectural, not a typo: a whole step is missing.

**The root cause.** `rate_song` never notifies the song's sharer. The codebase's pattern is "create the notification as a side effect inside the triggering action," and the rating action omits the side effect its sibling performs.

**My fix and side-effect check.** After the commit in `rate_song`, added the parallel block: if `song.shared_by != user_id`, `create_notification(..., notification_type="song_rated", ...)`. Guarding on `!= user_id` (like `add_to_playlist`) means rating your own song notifies no one. Verified via the new `tests/test_notifications.py`: rating another user's song yields exactly one `song_rated` notification; rating your own yields none.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** A seeded playlist ("Late Night Vibes") has 7 `playlist_entries`, but `get_playlist_songs(playlist.id)` returned only **6** dicts — the highest-`position` song was missing. `tests/test_playlists.py::test_playlist_returns_all_songs` also failed (`4 != 5`).

**How I found the root cause.** README maps this to `playlist_service.py`. `get_playlist_songs` orders songs by ascending `position`, then returns `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the last element.

**The root cause.** The `songs[:-1]` slice excludes the final (highest-`position`) song every call — a plain off-by-one that contradicts the function's own docstring ("returns all songs in the playlist").

**My fix and side-effect check.** Removed the slice so it iterates over `songs`. The 7-entry playlist now returns 7 in order. Re-ran `tests/test_playlists.py`: all pass, including order and the empty-playlist case (`songs` on an empty list is still `[]`), and a single-song playlist now returns 1 instead of 0.

---

## Regression Test

`tests/test_notifications.py` covers Issue #4. `test_rating_notifies_sharer` fails on the pre-fix code (no notification is created) and passes after the fix; `test_rating_own_song_does_not_notify` guards the boundary so rating your own song never notifies. This test would have caught Issue #4 before it shipped — there was previously no test exercising the rate → notification path at all.
