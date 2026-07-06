# Project 5 — Mixtape Bug Hunt: Submission

**Course:** AI201 · **Branch:** `bugfix/mixtape`

All five issues were fixed (three required + two stretch), each as its own `fix:` commit, plus a regression test. `pytest tests/` → **15 passed**.

---

## AI Usage

I used an AI assistant (Claude) primarily for **codebase navigation and diagnosis verification**, not for generating fixes. Concretely:

- **Orientation.** I asked the AI to summarize each `services/*.py` file ("what is this module responsible for, and what does each function do?") and to trace two call chains — rate-a-song and view-a-playlist — from route → service → model. This produced the first draft of my codebase map, which I then verified by reading each file myself.
- **Data-flow tracing.** I asked it to trace "how does adding a song to a playlist produce a notification?" so I'd have a known-good reference pattern to compare against the rating path. That side-by-side comparison is what made Issue #4 obvious to me.
- **Targeted concept check.** For Issue #1 I asked "what's the difference between Python's `datetime.weekday()` and `isoweekday()`?" — but only *after* I had already narrowed the bug to the Sunday guard on line 73. I used the answer (`weekday()` returns 6 for Sunday) to confirm my read, then verified by calling `update_listening_streak` with a Sunday datetime and watching the streak reset.
- **Where the AI was wrong, and I had to course-correct.** The AI confidently claimed Issue #3 would produce visibly duplicated rows and that the fix was simply "add `.distinct()`." When I reproduced it, `search_songs("Crown")` returned exactly **one** row and `tests/test_search.py` already passed — no duplicate at all. Reading the code and testing myself revealed the real situation: SQLAlchemy's legacy `db.session.query(Song)` de-duplicates full entities by primary key, so the stray `outerjoin(song_tags)` is a *latent* duplication bug (the raw join yields 3 rows for a 3-tag song) that the ORM happens to mask. The AI's line-level hunch was in the right file, but its claim about the observable behavior — and the reason — was wrong. I only caught that by running the query at the SQL level (`.count()` → 3) myself.

Every diagnosis below was confirmed by me reproducing the bug; the AI was a navigation and rubber-duck tool, not the author of the fixes.

---

## Codebase Map

*(Written during orientation, before opening any issue.)*

Mixtape is a Flask JSON API built with the **application-factory** pattern and a strict **route → service → model** separation.

### Main files and their roles

- **`app.py`** — The app factory. `create_app(config=None)` configures SQLite, initializes the shared `db = SQLAlchemy()` singleton, registers four blueprints under the URL prefixes `/songs`, `/playlists`, `/users`, `/feed`, and calls `db.create_all()`.
- **`models.py`** — Defines **7 models** — `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification` — plus **3 association tables**: `friendships` (symmetric user↔user), `song_tags` (song↔tag), and `playlist_entries`. `playlist_entries` is more than a join table: it carries a **`position`** column, so songs in a playlist have an explicit order, not just insertion order. `User.friends` is a self-referential dynamic relationship over `friendships`. Every model exposes a `to_dict()` used to build JSON responses.
- **`routes/`** — Thin controllers. Each route parses the request, calls exactly one service function, and `jsonify`s the result. `songs.py` (search / get / rate / listen), `playlists.py` (create / get / list-songs / add-song), `users.py` (profile / streak / notifications / mark-read), `feed.py` (listening-now / activity).
- **`services/`** — Where all business logic lives:
  - `streak_service.py` — records listening events and computes the consecutive-day listening streak.
  - `feed_service.py` — the "Friends Listening Now" feed (recent activity, deduped to the most-recent song per friend) and a general activity feed.
  - `search_service.py` — case-insensitive song search by title/artist, plus single-song lookup.
  - `notification_service.py` — creates and retrieves notifications; also holds `add_to_playlist` (which, after adding a song, notifies the song's original sharer) and `rate_song`.
  - `playlist_service.py` — playlist creation and ordered song retrieval (by `position`).
- **`seed_data.py`** — Recreates all tables and inserts 5 users, 10 tags, 13 songs grouped by tag count (0 / 1 / 3+ tags), friendships, a mix of recent (~10–20 min) and older (2 h – 14 day) listening events, per-user `last_listened_at` values, 3 playlists of 5–7 songs, and a sample notification.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`, each with a local in-memory-SQLite `app` fixture (there is no `conftest.py`) that calls service functions directly.

### Data-flow trace — "a user rates a song"

1. Client sends `POST /songs/<song_id>/rate` with JSON `{ "user_id", "score" }`.
2. **`routes/songs.py`** `rate()` parses `user_id`/`score`, checks they're present, and calls `notification_service.rate_song(user_id, song_id, score)`.
3. **`notification_service.rate_song()`** validates the score is 1–5, loads the `Song` and the rater (raising `ValueError` if either is missing), then **upserts** a `Rating`: if a `(user_id, song_id)` row already exists it updates the `score`, otherwise it inserts a new `Rating` (the model has a unique constraint on `user_id`+`song_id`). It commits and returns the `Rating`.
4. The route serializes the rating to JSON.

A useful architectural comparison is the sibling method `add_to_playlist()`, which follows the same route→service shape and, after writing its change, calls `create_notification(user_id=song.shared_by, ...)`. This reflects the app's general convention: **a notification is created as a side effect inside the action that triggers it**, rather than by any central dispatcher.

### Patterns I noticed

- Routes never touch the DB directly (beyond a trivial user lookup) — all logic is in services, so tracing any behavior is: find the route → read its one service call → read that function.
- Notifications are side effects of actions, so a missing notification shows up as a *missing block* in a service method, not as a broken function.
- Association tables carry real data (`playlist_entries.position`), and services query them explicitly with `.join(...)` rather than relying only on ORM relationships.

---

## Root Cause Analyses

*Each code fix is its own `fix:` commit on `bugfix/mixtape`; the five entries below cover Issues #1–#5.*

### Fixes at a glance

| Issue | File | Root cause (one line) | Change | Commit message |
|---|---|---|---|---|
| #1 | `services/streak_service.py` | `weekday() == 6` on Sundays routed consecutive listens into the reset branch | removed `and today.weekday() != 6` | `fix: increment listening streak on consecutive Sunday listens` |
| #2 | `services/feed_service.py` | "listening now" window was 24 h | `RECENT_THRESHOLD` 24 h → 30 min | `fix: narrow "listening now" feed window from 24h to 30 minutes` |
| #3 | `services/search_service.py` | stray `outerjoin(song_tags)` multiplies rows per tag (latent, masked by ORM de-dup) | removed the `outerjoin` line | `fix: de-duplicate song search by dropping unnecessary tag outerjoin` |
| #4 | `services/notification_service.py` | `rate_song` never called `create_notification` | added the `song_rated` notification block | `fix: notify song sharer when their song is rated` |
| #5 | `services/playlist_service.py` | `songs[:-1]` dropped the last song | `songs[:-1]` → `songs` | `fix: include the final song in playlist listing` |

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** I called `update_listening_streak(user, now)` directly with a user whose `last_listened_at` was a Saturday and `now` set to the following Sunday (`datetime(2026, 7, 5, ...)`, which `.weekday()` confirms is `6`). The streak reset to `1` instead of incrementing to the next value. (Today, 2026-07-05, is itself a Sunday, so `POST /songs/<id>/listen` reproduces it live too.) The existing `tests/test_streaks.py::test_streak_increments_on_sunday` also fails, asserting `2 == 1`.

**How I found the root cause (navigation).** The README maps this to `streak_service.py`. I read `update_listening_streak` top-down, tracking each branch: `days_since_last == 0` → no change; the consecutive-day branch → `elif days_since_last == 1 and today.weekday() != 6:`; else → reset to 1. The `and today.weekday() != 6` clause is what stopped me — a streak rule has no business asking what day of the week it is. That was the moment of certainty: on Sundays this `elif` is false, so a consecutive listen silently falls through to the reset branch.

**The root cause.** Python's `datetime.weekday()` returns `6` for Sunday. Because of the `and today.weekday() != 6` guard, when a user's consecutive listen lands on a Sunday the `elif` evaluates false and control drops into `else`, which sets `listening_streak = 1`. So the streak resets every Sunday even though the user listened the day before. (This is the classic `weekday()` [Mon=0…Sun=6] vs `isoweekday()` [Mon=1…Sun=7] confusion — but the correct rule doesn't need a weekday check at all: "listened yesterday" already fully determines a consecutive day, independent of which day it is.)

**My fix.** Removed the spurious clause so the branch reads simply `elif days_since_last == 1:`. A consecutive-day listen now increments regardless of weekday.

**Side-effect check.** My change only deletes a condition from the `days_since_last == 1` branch, so the risk is that I altered the first-listen or reset paths. I re-ran `tests/test_streaks.py` and confirmed all four other behaviors are intact: new user → streak 1, same-day listen → no change, consecutive day → +1, and a multi-day gap → reset to 1. The reset branch (which handles gaps > 1 day) is untouched, so genuine streak breaks still reset.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Viewing the feed as **darius** — `get_friends_listening_now(darius.id)` — returned two friends: simone (listened ~15 min ago) *and* nova, whose most recent listen was **~2 hours ago**. A feature called "Listening **Now**" surfacing someone from 2 hours ago is precisely the reported behavior.

**How I found the root cause (navigation).** The README maps this to `feed_service.py`. I traced `get_friends_listening_now`: it computes `cutoff = datetime.now(utc) - RECENT_THRESHOLD`, queries `ListeningEvent` rows with `listened_at >= cutoff` for the user's friends, then dedupes to the most recent event per friend. The query, the `>=` boundary, and the dedup were all correct — the only thing that defines "recent" is the module-level constant. Reading line 13, `RECENT_THRESHOLD = timedelta(hours=24)`, made it clear the window itself was the defect.

**The root cause.** `RECENT_THRESHOLD` was `timedelta(hours=24)`. "Listening now" is meant to be a live-presence window measured in minutes, but a 24-hour cutoff admits any friend who listened at any point in the last day. The boundary comparison worked exactly as written — the constant was simply defined a thousand-fold too wide for the feature's meaning, so "active in the last day" was being reported as "listening now."

**My fix.** Changed the constant to `timedelta(minutes=30)`, a window consistent with a live "now" feed.

**Side-effect check.** The concrete risk is over-shrinking the window and hiding genuinely-active listeners. I verified the seed's recent events (10, 15, and 20 minutes old) all still fall inside 30 minutes, so real "now" listeners remain — re-running the feed as darius returns simone (15 min) and correctly drops nova (2 h). I also checked the other function in the same file, `get_activity_feed`: it deliberately returns history regardless of recency and never reads `RECENT_THRESHOLD`, so narrowing the window cannot affect it.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it.** This one is subtle. `search_songs("Crown")` returns exactly **one** row and `tests/test_search.py` already passes — the user-facing duplicate does **not** currently manifest. To confirm the defect is nonetheless real, I ran the underlying query at the SQL level: the `outerjoin(song_tags)` produces **3 rows** for the 3-tag song "Crown Heights Anthem" (`db.session.query(Song.id).outerjoin(song_tags, ...).filter(...).count()` → 3). So the duplication exists in the query and is only being hidden.

**How I found the root cause (navigation).** The README maps this to `search_service.py`. Reading `search_songs`, the query is `db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(title/artist ilike ...).all()`. I noticed the `WHERE` clause never references tags — so the join can only *multiply* rows by a song's tag count, never filter them. That raised the question "why aren't there duplicates, then?" Testing at the ORM vs SQL level answered it: SQLAlchemy's **legacy `Query`, when selecting a single mapped entity, de-duplicates results by primary key**, while a raw `.count()` over the same join does not. That contrast was the moment I was sure the join was the culprit.

**The root cause.** An unnecessary `outerjoin(song_tags)` with no `.distinct()`. A song with N tags maps to N joined rows; the function returns one row per song *only* because it accidentally relies on legacy-`Query` entity de-duplication. It's a latent bug: the moment the query is executed with 2.0-style `select()` (which does **not** auto-unique), or via `.count()`, or by selecting columns instead of the whole entity, the duplicates become real and user-visible. The correct behavior — one row per matching song — should be guaranteed by the query, not left to an ORM side effect.

**My fix.** Removed the `.outerjoin(song_tags, ...)` line entirely. The filter never used tags, so the join was pure liability; search now returns one row per matching song by construction.

**Side-effect check.** The obvious risk of deleting the tags join is losing tag data from the results. I confirmed that can't happen: each song's tags are loaded from the `Song.tags` relationship inside `to_dict()`, independently of this query, so every result dict still includes its `tags` list. I re-ran `tests/test_search.py`: title/artist matching, the no-tag / one-tag / multi-tag single-appearance cases, and the empty-result case all pass.

### Issue #4 — Notified when a friend adds my song to a playlist, but not when they rate it

**How I reproduced it.** I had kenji rate a song that simone shared — `rate_song(kenji.id, song_shared_by_simone.id, 5)` — and then checked `get_notifications(simone.id)`. It was empty both before and after the rating. For contrast, calling `add_to_playlist(...)` on the same song *does* create a notification for simone, confirming the behavior is specific to the rating path.

**How I found the root cause (navigation).** The README maps this to `notification_service.py`, which conveniently contains both `add_to_playlist` and `rate_song`. I read them side by side. `add_to_playlist` ends with `if song.shared_by != added_by_user_id: create_notification(...)`. `rate_song` validates the score, upserts the `Rating`, commits, and returns — with no `create_notification` call anywhere in it. Seeing the working method notify and the broken one simply not have that block is what told me this was architectural (a missing step), not a typo in existing logic.

**The root cause.** `rate_song` never notifies the song's original sharer. In this codebase the convention is that a notification is created as a side effect *inside* the action that triggers it; the rating action just omits the side effect that its sibling `add_to_playlist` performs. Nothing is broken in the code that *is* there — an entire behavior is absent.

**My fix.** After the commit in `rate_song`, I added the block that mirrors `add_to_playlist`: if `song.shared_by != user_id`, call `create_notification(user_id=song.shared_by, notification_type="song_rated", body=...)`.

**Side-effect check.** The specific risk here is *over*-notifying — a user rating their own shared song, or re-rating via the upsert path, shouldn't ping the sharer. I guarded on `song.shared_by != user_id` exactly as `add_to_playlist` does, and my new `tests/test_notifications.py` asserts both directions: rating another user's song produces exactly one `song_rated` notification, and rating your own produces zero. The rating write itself (score validation, insert-vs-update) is unchanged, so existing rating behavior is preserved.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** The seeded playlist "Late Night Vibes" has 7 rows in `playlist_entries`, but `get_playlist_songs(playlist.id)` returned only **6** song dicts — the highest-`position` song was always missing. The existing `tests/test_playlists.py::test_playlist_returns_all_songs` also fails (`4 != 5`).

**How I found the root cause (navigation).** The README maps this to `playlist_service.py`. I read `get_playlist_songs`: it queries `Song` joined to `playlist_entries`, ordered by ascending `position`, into a list `songs`, then returns `[song.to_dict() for song in songs[:-1]]`. The query and ordering were correct; the `[:-1]` slice on the final line is the entire bug.

**The root cause.** The `songs[:-1]` slice drops the last element of the position-ordered list on every call, so the highest-`position` (most recently added) song is always excluded. It's a plain off-by-one, and it directly contradicts the function's own docstring, which states it "returns all songs in the playlist."

**My fix.** Removed the slice so the comprehension iterates over `songs` instead of `songs[:-1]`.

**Side-effect check.** The plausible boundary risk is the empty and single-song cases. I confirmed an empty playlist still returns `[]` (iterating an empty list yields nothing — and the old `[:-1]` also returned `[]` there, so there's no regression), and that a one-song playlist now correctly returns 1 dict instead of 0. I re-ran `tests/test_playlists.py`: the all-songs count, the position-ordering test, and the empty-playlist test all pass.

---

## Regression Test

I added `tests/test_notifications.py`, which covers Issue #4 — a path that previously had **no** test at all:

- `test_rating_notifies_sharer` — a user rating another user's shared song produces exactly one `song_rated` notification for the sharer. This test **fails on the pre-fix code** (the notification is never created) and passes after the fix, so it would have caught the bug before it shipped.
- `test_rating_own_song_does_not_notify` — rating your own shared song produces no notification, locking in the `song.shared_by != user_id` boundary so a future change can't reintroduce self-notifications.

Run with `pytest tests/` (all 15 tests pass, including the two above).

---

## Verification

Beyond the test suite, I confirmed each fix's *behavior* by calling the service functions directly against the seeded database and checking the exact post-fix outcome:

- **#1** — `update_listening_streak` with `last_listened_at` = Saturday and `now` = Sunday now yields `streak + 1` (was resetting to 1).
- **#2** — `get_friends_listening_now(darius.id)` now returns only simone (~15 min); nova (~2 h) is correctly excluded.
- **#3** — `search_songs("Crown")` returns exactly one row, and that row still carries all 3 of its tags — confirming the removed join was never what supplied the tags (they come from the `Song.tags` relationship), so dropping it lost nothing.
- **#4** — a cross-user rating creates exactly one `song_rated` notification; a self-rating creates none.
- **#5** — the 7-entry playlist returns 7 songs; empty → `[]`; single-song → 1.

`pytest tests/` → **15 passed**.
