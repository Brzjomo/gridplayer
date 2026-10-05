# Streaming and network

Opening a URL, resolving it, and the local proxy that relays it to VLC.

## Why this subsystem is complicated

Three different clients fetch on the app's behalf, and none of them agrees with
the others about how it is configured. `utils/network.py` opens with the summary:

> yt-dlp takes a dict of options, Streamlink takes a session to mutate, and VLC
> takes command line flags.

And VLC is the problem child:

> A user agent is the end of what it will honour from here: it finds a proxy for
> itself and cannot be told not to, and it has no notion of an address family or
> of whose certificates to trust. So a setting VLC cannot carry out is answered
> the way a cookie already is — the link stops being handed to VLC and goes
> through the stream proxy instead.

So there are two delivery paths:

| Path | When | VLC gets |
| --- | --- | --- |
| **Direct** | Nothing special needed | The real URL, fetched by libVLC itself. |
| **Relayed** | A cookie is needed, or a network setting VLC cannot honour | A `http://127.0.0.1:<port>/…` URL of ours. |

Everything else in this document follows from that split.

## The resolution pipeline

`open_url` on a `VideoBlock` leads to `VideoURLResolver`, which is a `QThread`
wrapper:

```text
VideoBlock.load_video_url()
  └── VideoURLResolver.resolve(url)          url_resolve.py:150
        └── _resolve_url signal ──► worker on its QThread
              └── resolve_url(url, on_status)          url_resolve.py:47
                    ├── _pick_resolvers(url)          which resolver, in what order
                    └── resolver(url).resolve()       → ResolvedVideo
              └── url_resolved signal back to the main thread
```

### Resolver selection

`_pick_resolvers` (`url_resolve.py:205`):

1. **YouTube short-circuits.** `_is_match_youtube` asks yt-dlp's own extractor
   classes (`YoutubeIE`, `YoutubeYtBeIE`, `YoutubeLivestreamEmbedIE`,
   `YoutubeClipIE`) whether they claim the URL, and if so **only yt-dlp is
   tried**.
2. Otherwise `_get_resolvers` builds an ordered dict: the **priority resolver
   first**, then the rest. The priority comes from
   `streaming/resolver_priority`, overridden per-URL by
   `streaming/resolver_priority_patterns` (`ResolverPatterns.get_resolver`).
3. Each resolver's `is_able_to_handle(url)` is consulted and the ones that say no
   are dropped.
4. `resolve_url` then tries them **in order**, returning the first success, and
   swallowing `NoResolverPlugin` to fall through to the next.

`DirectResolver.is_able_to_handle` always returns `True`, so it is the terminal
fallback — unless it is not in the chain because a pattern moved it.

### Resolvers

| Class | File | Notes |
| --- | --- | --- |
| `ResolverBase` | `resolver_base.py:22` | Abstract: `title`, `is_live`, `streams`, and optional `chapters`, `duration_ms`, `youtube_id`. `resolve()` packs them into a `ResolvedVideo`. |
| `DirectResolver` | `resolver_base.py:73` | Hands the URL over as-is, unless `needs_relay(url)` says otherwise. |
| `StreamlinkResolver` | `resolver_streamlink.py` | Streamlink plugins. |
| `YoutubeDLResolver` | `resolver_yt_dlp.py:89` | The big one (~780 lines). |

`ResolverBase` has **default implementations for the optional three**, returning
empty. That is the extension point: a new resolver implements the three required
properties and overrides only what it can actually provide.

### Imports are deliberately lazy

```python
def _resolver_map() -> dict[URLResolver, type["ResolverBase"]]:
    from gridplayer.utils.url_resolve.resolver_base import DirectResolver
    ...
```

The comment at `url_resolve.py:32` explains: **streamlink and yt-dlp take about a
third of a second to import**, so they are deferred until a URL actually needs
resolving, on the worker thread — instead of holding up every startup whether it
opens a URL or not. `StreamProxyManager.stream_proxy` does the same for the proxy.
If you add a module that imports yt-dlp or streamlink at module scope, you have
just slowed every startup.

### Threading and abandonment

`VideoURLResolver` runs the work on a dedicated `QThread`. The subtlety is
teardown (`url_resolve.py:131`):

```python
def cleanup(self):
    ...
    self.thread.quit()
    # a resolver can't be interrupted, and yt-dlp on a slow host can
    # take its time, so waiting for it here freezes whoever is closing
    if self._is_busy:
        _abandon(self.thread, self.worker)
    else:
        self.thread.wait()
```

**A resolver cannot be cancelled.** So if one is mid-flight when the video is
closed, the thread is *abandoned* rather than waited on — and held in the
module-level `_abandoned` set, because:

> a QThread dropped while it is still running takes the whole app down with it.

`_abandon` connects `thread.finished` to a release callback and also checks
`thread.isFinished()` immediately, since the thread may have stopped before
anyone could be told. `_on_resolved` / `_on_error` / `_on_status` all check
`_is_closed` because results posted just before cleanup still arrive after it.

**This is the pattern to copy for any other uncancellable background work.**

## Network settings

`utils/network.py` is the single place settings become client options. The
frozen `NetworkOpts` dataclass (`network.py:63`) is both the carrier and the
change-detection key — being frozen is what lets it answer "have the settings
changed since?".

`network_opts()` reads the six settings; `opts_for(...)` builds the same object
from explicit values, which is **how the settings page's checkups test what is on
screen rather than what was saved**.

Two decisions worth understanding:

**`Custom` proxy with an empty URL means no proxy, not the system one**
(`network.py:149`):

> the user has said they want one of their own, and falling back to the one they
> were moving away from would be answering a half-finished form with the
> opposite of what it says.

**`is_relay_required`** (`network.py:90`) is the relay trigger. It is true when
the proxy mode is *anything other than System*, or IPv4 is forced, or TLS
verification is off. The docstring explains why refusing the system proxy counts:

> It is the only way to promise that nothing goes through one: VLC finds a proxy
> for itself, out of the environment on every platform and out of the registry on
> Windows, and there is no flag that stops it looking.

A timeout is explicitly *not* a relay reason — "nobody's business but the
client's".

### Applying to each client

| Client | Function | How |
| --- | --- | --- |
| Streamlink | `apply_to_streamlink` (`:202`) | Mutates a session: `http.trust_env`, `http.proxies`, `http.verify`, timeout, user agent, `set_option("ipv4", …)`. |
| yt-dlp | `ytdl_network_opts` (`:270`) | Returns an options dict, **omitting** keys left alone so yt-dlp decides for itself. |
| VLC | `vlc_user_agent` (`:306`) | A user agent, and that is all. |
| Both | `configure_session` (`:187`) | Cookies **and** network settings together, "so a new place that reaches out cannot pick up the cookies and forget the proxy". |

`apply_to_streamlink` is safe to call repeatedly on a live session, and
`_original` (`:351`) is why: the pre-existing user agent and timeout are stashed
on the session the first time, so **clearing a setting hands back what the session
came with** rather than leaving the last value we wrote. The comment notes a
format's own header goes on after us and is not ours to remember.

The IPv4 handling has a documented wart (`network.py:234`):

> Streamlink's ipv4 option only ever turns the restriction on: set to False on a
> session that never had it on, it leaves whatever the last session turned on
> still standing. And what stands is a urllib3 global rather than anything this
> session owns, so not forcing it has to be said outright or it means "whatever
> was asked for last".

Hence the explicit `set_address_family(AF_INET or None)`.

`fetch_capped` (`:243`) is for the checkups: it wants the first few bytes, not the
whole response, and it **re-raises the underlying error rather than Streamlink's
wrapper**, because the checkup distinguishes a refusing proxy from an untrusted
certificate by exception type. It imports `streamlink.exceptions` inside the
function — same lazy-import reason.

## Cookies

`utils/cookies.py`, ~737 lines. Cookie storage is a **Netscape-format**
`cookies.txt` in the app data directory (`COOKIE_FILE_NAME`).

| Piece | Purpose |
| --- | --- |
| `CookieJar(MozillaCookieJar)` (`:104`) | The jar, with `RewindingBuffer` so the file can be re-read without reopening. |
| `CookieStore` (`:179`) | Load/save/mutate the jar on disk, with a lock. |
| `cookie_store()` (`:297`) | The process-wide singleton store. |
| `parse_cookies(text)` (`:444`) | Accepts **both** Netscape text and a JSON dump (browser-extension export). |
| `has_cookies_for(url)` (`:322`) | The relay trigger. Memoised (`CACHED_COOKIE_ANSWERS = 128`), keyed by the file stamp so an edit invalidates it. |
| `apply_to_streamlink(session)` (`:355`) | Pours the jar into a Streamlink session. |
| `ytdl_cookies()` (`:398`) | The same cookies in yt-dlp's expected form. |
| `cookies_stamp()` (`:380`) | File mtime+size fingerprint, used in `session_stamp()` so a long-lived proxy session notices a login being added or removed. |
| `domain_summary(jar)` (`:421`) | The per-domain view the settings page's cookie list shows. |
| `merged_with` / `without_domains` (`:474`, `:488`) | Import merge and per-domain deletion. |

`_parse_json` plus `_cookie_from_mapping` / `_expiry_of` handle the three
different expiry key spellings found in the wild
(`EXPIRY_KEYS = ("expirationDate", "expires", "expiry")`), and
`_is_usable_line` tolerates the malformed lines real `cookies.txt` files contain.

Cookies are managed through the settings dialog's cookie list widget
(`widgets/cookie_store_list.py`), which can import from a file or from pasted
text and reports `CookieImportError`.

## The stream proxy

`utils/stream_proxy/` is a local HTTP server that fetches on VLC's behalf. It
exists because VLC cannot be told about cookies, proxies, address family or
certificate trust, and because manifests need rewriting for seamless relay.

### Lifecycle

`StreamProxyManager` (`player/managers/stream_proxy.py`) holds a lazily created
`StreamProxy`, guarded by a lock, and instantiates it **only on first use**:

```python
# the proxy is built on streamlink, which is slow to import,
# and most sessions never play a stream through it
```

`StreamProxy` is a `QThread` (`stream_proxy.py:10`) whose `run()` calls
`serve_forever()` on a `StreamProxyServer(("127.0.0.1", 0), …)` — **port 0 means
the OS picks a free port**, retrieved through `base_url` as
`http://127.0.0.1:<port>`. `cleanup()` shuts the server down and waits.

`StreamProxyServer` is a `ThreadingHTTPServer`, so each request gets its own
thread. It holds four lock-guarded registries:

| Registry | Key | Value |
| --- | --- | --- |
| `_sessions` | session id | a `StreamSession` |
| `_streams` | stream id | a `Stream` |
| `_bases` | base id | `(base_url, session_id)` for DASH |
| `_refresh_locks` / `_refreshed` | stream id | lock, and the last refresh result |

### Two ways a URL is handed out

`add_stream` (`server.py:99`) picks between them:

```python
is_by_id = fragment is not None or stream.is_complex or stream.is_refreshable

if is_by_id:
    # audio tracks and fragment lists do not fit into a query string,
    # and a stream that may be resolved again has to stay reachable
    # under the same id once its URLs have been replaced
    params = {"stream_id": ..., "session_id": ...}
else:
    params = {"url": ..., "protocol": ..., "session_id": ...}
```

* **Query form** (`/?url=…&protocol=…&session_id=…`) for simple single-URL
  streams.
* **By id** (`/?stream_id=…&session_id=…[&fragment=…]`) for anything complex,
  refreshable, or a single fragment. The id is stable, which is the point: when a
  signed URL expires the entry is **updated in place** and VLC keeps using the same
  local URL.

`add_fragments` (`:124`) is the bulk form, and the docstring explains why it
exists: "a long video has thousands of segments, and hashing a list of them once
per segment takes seconds".

IDs come from `generate_id`, a UUID3 over a namespace seeded per server instance
(`uuid4()`), hashing the token — so ids are stable within a run but not across
runs.

### DASH gets a path, not a query

```python
DASH_RELAY_PREFIX = "/dash/"
```

The comment (`server.py:30`) is the reason:

> It cannot go through the query string the rest of the proxy uses: a player
> resolves a segment against the base it was given, and resolving a relative URL
> against one with a query throws the query away.

`add_base` registers where the segments *will be* — "DASH names its segments with
a template that only the player can fill in, so there is no list of them to
register". `dash_query` reverses it, joining the template tail onto the base.

### Expiry, refresh and retry

Three constants encode the failure handling:

```python
EXPIRED_STATUSES = {401, 403, 410}   # a signed URL that ran out, forever
REFRESH_COOLDOWN = 10.0              # one re-resolve per burst
TRANSIENT_RETRIES = 2
TRANSIENT_RETRY_DELAY = 0.5
```

`RelayFailure` (`server.py:46`) classifies the two failure modes:

* `response is None` → **transient** ("the stream broke on the way there, which is
  the kind of thing that passes") and worth retrying.
* a response with an expired status → **expired**, and worth re-resolving.

`REFRESH_COOLDOWN` exists because "every fragment in flight fails at the same
moment, and they all want the same fresh URLs" — so one thread re-resolves while
the rest wait on the per-stream lock and reuse the result.
`TRANSIENT_RETRIES` exists because "dropped connections and hiccups on the way to
the CDN are common enough that giving up on the first one drops frames for no
reason".

The proxy re-resolves sources itself, by being handed `resolve_url` at
construction:

```python
self.server = StreamProxyServer(
    ("127.0.0.1", 0), ProxyRequestHandler, resolve_source=resolve_url
)
```

with the comment that this happens "on the thread that ran into the expired URL;
nothing on the UI side is waiting for it". That is exactly why `resolve_url` is a
**plain function** and not a method — see its docstring at `url_resolve.py:47`:
"the proxy calls it from its own threads to renew URLs that have expired, where
there is no Qt object to report to."

### Wrapper selection

`StreamSession.get_stream` (`session.py:41`) picks the wrapper. The mapping is
pinned by tests:

| Protocol | Wrapper |
| --- | --- |
| `http` | `HTTPStreamProxy` |
| `hls_proxy` | `HLSProxy` |
| `dash_proxy` | `DASHManifestProxy` |
| `http_hls` | `HTTPPlaylistStream` |
| `dash` | `DASHPlaylistStream` |
| `hls` | `HLSProxyLive` |

Two rules sit on top of the table:

* **Separate audio wins over protocol.** If `stream.audio_tracks` is set, it is
  always `HLSMuxedStream`, "because VLC can only be given both through a
  playlist, so that comes first". Tested by
  `test_a_stream_with_separate_audio_is_paired_whatever_its_protocol`.
* **An unknown protocol raises**, rather than being served by whichever wrapper
  happened to be last — the test asserts `RuntimeError` matching the protocol
  name.

The wrapper modules do the format-specific work:
`m3u8.py` (155 lines), `mpd.py` (DASH, 120), `mp4.py` (132), `fragments.py`,
`wrappers.py` (384, the wrappers themselves plus `StreamReader`).

### Session settings stay live

`StreamSession` keeps a `session_stamp()` and re-applies settings when it changes
(`_sync_settings`, `session.py:59`):

> A session is kept for as long as the proxy runs and shared by every stream from
> the same service, so a login added or taken away, or a proxy switched on, would
> otherwise not be noticed until a restart.

And header ordering is deliberate (`_apply_settings`, `:78`):

> The headers a format came with go on last and stay the last word. A site signs
> an address for the user agent that asked it for one, so a shared default laid
> over that would be asking for the bytes as somebody else.

## yt-dlp specifics

`YoutubeDLResolver` (`resolver_yt_dlp.py:89`) is the largest resolver. Its job is
turning yt-dlp's format list into GridPlayer's `Streams`, and most of its ~780
lines are naming, de-duplication and protocol classification helpers.

Notable behaviours:

* **`_collapse_manifest_rungs`** (`:518`) merges manifest variants that are rungs
  of the same ladder, and `_carries_its_own_sound` / `_manifest_key` support it.
* **Protocol classification** — `_get_stream_protocol` (`:408`),
  `_manifest_protocol` (`:431`), `_is_dash_container`, `_is_hls_segmentable`,
  `_is_segmentable_file`, `_is_playlist_capable`.
* **`_needs_impersonation`** (`:444`) — some hosts require browser TLS
  impersonation, which is why `curl-cffi` is a dependency.
* **`_fix_bitrate_units`** (`:670`) with `_is_bitrate_in_bits` — yt-dlp reports
  bitrate in bits for some extractors and bytes for others, and the whole list is
  checked to decide which.
* **`_get_fragments`** (`:470`) builds the fragment list for segmented sources.
* **Naming** is a small sub-language: `_get_video_fmt_name`, `_get_audio_fmt_name`,
  `_get_fmt_name`, `_unique_name`, and `_get_language` / `_is_original_language`
  for the multilingual case.
* **Chapters** come through `_get_chapters` / `_chapter_name`.

### JavaScript runtime discovery

YouTube scrambles stream URLs with a script that must be executed to undo, and
yt-dlp runs it by shelling out to Deno, Node, QuickJS or Bun. `utils/js_runtime.py`
exists because **yt-dlp only searches `PATH`, which is wrong for nearly every way
GridPlayer ships**. The module docstring lists the cases:

* a **snap** is strictly confined — the host's `/usr/bin` is absent, and the home
  interface refuses hidden folders, which is where Deno's installer and nvm put
  things;
* a **flatpak** has the runtime's own `PATH`; the host's copy is readable at
  `/run/host` but nothing searches there;
* a **macOS app started from Finder** inherits launchd's `PATH`, not the shell's,
  so Homebrew and `~/.deno/bin` are both missing.

`js_runtime_dirs()` searches, best first: the folder named in settings →
the GridPlayer data folder (where a snap must be given one) → `/run/host/usr*/bin`
inside a flatpak → `/opt/homebrew/bin`, `/usr/local/bin` on macOS → `~/.deno/bin`,
`~/.bun/bin`.

Two details:

* **`JS_RUNTIME_BINARIES` maps engine name → binary name**, because they differ:
  quickjs ships as `qjs`. An engine missing from the map is simply left to
  `PATH`.
* **`ytdl_js_runtimes` names every engine, not one.** Naming all of them is not a
  vote: yt-dlp ranks them and takes the best that answers; left alone it looks for
  Deno only, so a machine with Node would be told there is none. An engine is
  given a **path only where one was found**, because a path replaces `PATH` rather
  than adding to it — so unfound engines are handed `{}` and searched normally. A
  fresh dict is returned each time because yt-dlp prunes the one it is given.

`_find_js_runtime` uses `is_file()` rather than running the binary, because
"asking a binary its version costs a process, and this happens on the way to
every resolve". `~/.nvm` is explicitly *not* searched, since nvm keeps a folder
per version rather than one to look in.

### `untangle_percent_re`

Several call sites call `untangle_percent_re()` from `utils/percent_re.py`
*immediately after* importing yt-dlp, with the comment "yt-dlp patches urllib3 as
it is imported". It works around a yt-dlp/urllib3 incompatibility with percent
signs in regexes. **Call it after importing yt-dlp anywhere new.**

## SponsorBlock

`utils/sponsorblock.py`. The design constraints are stated up front:

> The segments are looked up once a video has resolved, on a thread of their own,
> and arrive when they arrive: a video is never kept waiting on them. Not in the
> resolver, which the stream proxy also runs to renew a link that has expired, and
> which would then look the video up again every time; and not on the video's
> resolver thread either, where a slow answer would hold up resolving whatever
> that video is switched to next.

So: a **separate daemon thread**, a cache, and a signal.

| Piece | Purpose |
| --- | --- |
| `SponsorBlockFetcher` (`:192`) | One lookup per video at a time, shared by every cell playing it; `fetched(video_id, segments)` signal. |
| `LOOKUP_CACHE_SEC = 3600` | A grid of the same video, a reload, the auto-reload timer and a switch back all ask again; the database changes more slowly. |
| `LOOKUP_RETRIES = 1` | "one more try before a video goes without, rather than yt-dlp's three". |
| `fetch_segments` (`:80`) | Uses yt-dlp's `SponsorBlockPP`, asking **by the first characters of a hash of the video ID**, as SponsorBlock requires. |
| `skip_spans` (`:156`) | Turns segments marked `SKIP` into merged `SkipSpan`s. |
| `skip_span_at` (`:182`) | Which span a time falls in. |

yt-dlp does the asking, "because it knows the API, goes out with the network
settings everyone else here does".

Three timing constants encode UX care:

* `SKIP_JOIN_MS = 1000` — "Users mark a sponsor read and the plug after it
  separately, and they seldom meet exactly; a seek on a stream costs a second or
  so, and there is no sense in landing for a moment between the two only to leave
  again."
* `SKIP_MIN_MS = 1000` — nothing is skipped with less than this left, "on a stream
  the seek would take longer to land than the rest of it takes to play out".
* `SKIP_END_MARGIN_MS = 1000` — a span ending within this of the video's own end
  ends the pass instead of being skipped.

`fetch_segments` asks for **every category regardless of settings**, "so that a
change to them is a change to what is shown rather than a reason to ask again" —
which is why `VideoBlock` merges segments into seek marks and re-derives the skip
spans locally when settings change.

`parse_segments` (`:120`) is defensive with a stated principle: "a database is no
reason for a video not to play". Unknown categories, non-finite times and negative
starts are dropped; a `HIGHLIGHT` segment is collapsed to its start because
"yt-dlp stretches a highlight out to a second, to mark it in a file".

A failed lookup is **not cached** (`_on_looked_up`, `:250`): "a failure is not
remembered, the next play of it asks again".

Category colours and the enabled flag live in `params/sponsorblock.py`, which also
provides `category_setting(category)` — the `SponsorBlock` settings keys.

## Checkups

Two diagnostic tools back the settings page, both run from `dialogs/settings.py`:

| Tool | File | Answers |
| --- | --- | --- |
| `YouTubeCheckup` | `utils/ytdlp_checkup.py` (667 lines) | "Resolve a real link the way playback would, step by step". |
| `NetworkCheckup` | `utils/network_checkup.py` (595 lines) | Whether the proxy/TLS/UA settings actually work. |

Both are shown in a `CheckupDialog` (`dialogs/checkup.py`, 329 lines) that walks
through steps with pass/fail detail. Both take their settings **from the dialog's
widgets, not the store**, which is why `opts_for` and the `configured` parameter
of `js_runtime_dirs` exist.

The YouTube checkup's docstring notes "two of its six steps are about cookies and
the other four are about whether a link resolves at all, which is what this page
is for". Both import yt-dlp/streamlink lazily inside `run_*` because the settings
dialog module is imported at startup.

## Gotchas

* **Never import yt-dlp or streamlink at module scope** in anything on the startup
  path — it costs ~⅓ second on every launch. Follow the lazy-import pattern.
* **Call `untangle_percent_re()` right after importing yt-dlp.**
* **`resolve_url` must stay a plain function.** The proxy calls it from non-Qt
  threads with no event loop.
* **Never wait on a resolver thread.** Abandon it (`_abandon`) or you freeze the
  UI on close.
* **A relayed URL is per-stream-id and updated in place.** Do not mint a new local
  URL when a signed URL expires; update the registry entry.
* **`add_fragments` for bulk, not `add_stream` per segment** — hashing per segment
  is quadratic and takes seconds on a long video.
* **DASH must go through `/dash/`, not the query string**, or relative segment
  resolution drops the query.
* **Cookie changes must invalidate `cookies_stamp()`** or a long-lived proxy
  session will keep fetching with a stale login.
* **Retry only transient failures.** A 401/403/410 is expiry (re-resolve); no
  response is transient (retry). Retrying an expiry just burns time; re-resolving
  a transient failure does nothing.
* **SponsorBlock must never block resolution.** It is deliberately a separate
  thread with a cache.
* **Tests must not touch the real cookie jar or the real proxy setting** — the
  autouse `_no_real_cookies` and `_no_real_network_settings` fixtures in
  `conftest.py` exist for exactly that.

## Key files

| File | Role |
| --- | --- |
| `utils/url_resolve/url_resolve.py` | Resolver selection, the worker thread, abandonment. |
| `utils/url_resolve/resolver_base.py` | `ResolverBase`, `DirectResolver`. |
| `utils/url_resolve/resolver_yt_dlp.py` | yt-dlp → `Streams`. |
| `utils/url_resolve/resolver_streamlink.py` | Streamlink resolution. |
| `utils/url_resolve/stream_detect.py` | Is a direct URL a live stream? |
| `utils/network.py` | Settings → client options; `needs_relay`. |
| `utils/cookies.py` | Cookie store, import/export, stamps. |
| `utils/js_runtime.py` | Finding a JavaScript engine for yt-dlp. |
| `utils/sponsorblock.py` | Segment lookup, caching, skip spans. |
| `utils/stream_proxy/server.py` | The proxy server, IDs, expiry/refresh/retry. |
| `utils/stream_proxy/session.py` | Protocol → wrapper, live settings sync. |
| `utils/stream_proxy/wrappers.py`, `m3u8.py`, `mpd.py`, `mp4.py`, `fragments.py` | Format-specific relay and rewriting. |
| `utils/stream_proxy/stream_proxy.py` | The `QThread` wrapper. |
| `player/managers/stream_proxy.py` | Lazy singleton manager. |
| `models/stream.py` | `Stream`, `Streams`, quality selection, languages. |
| `utils/ytdlp_checkup.py`, `utils/network_checkup.py` | The settings-page diagnostics. |
| `params/sponsorblock.py` | Categories, colours, settings keys. |
