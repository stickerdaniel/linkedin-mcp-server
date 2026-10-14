# Daemon measurements

What has been measured or observed about the shared-browser daemon
(`linkedin_mcp_server/daemon/`), and which code each observation justifies.

Every number here is an observation, not a guarantee. Each entry names the
commit that recorded it and that commit's date, which is the closest record of
when the measurement was made. A dependency version named in an entry is the
version measured against; the project may have moved past it since. Code
comments point here instead of restating these numbers. When a re-measurement
changes a number, edit the entry and give its new source and date.

Symbols are named relative to `linkedin_mcp_server`. Tests are named as
`path::Class::test`.

## Lock

- **Unlocking before closing frees the lock for every holder.** Unlock-then-close
  left the lock free while a live process believed it held it, on both POSIX and
  Windows. Justifies `daemon.lock.DaemonLock.release` closing rather than
  unlocking. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_lock.py::TestReleaseSemantics::test_releasing_closes_rather_than_unlocks`.
- **Chromium outlives a free lock only briefly.** An earlier note claimed "over
  twenty seconds". Re-measuring found 13-38 ms on one machine and 11-103 ms
  independently, because Chromium is wired to its driver by
  `--remote-debugging-pipe` and exits when the pipe breaks. The rule in the
  `daemon.lock` docstring that a free lock is no proof nothing runs stands
  regardless. Source `6d369701`, 2026-07-29 (the old figure dates from
  `50dfdbf5`, 2026-07-28).
- **No two browsers in a direct collision test.** Five rounds with a successor
  pre-armed and spinning produced no round with two live browsers on one
  profile. Source `6d369701`, 2026-07-29.
- **A hard kill on Windows frees the lease before the browser dies, and no
  successor overlaps it.** A contender, armed before the kill, raced a hard
  kill of the owner and of a frozen Direct server. In both lanes the lease was
  free within 10 ms of the kill, before the old browser was gone. The last old
  browser process was gone after 36-62 ms (Direct) and 59-90 ms (owner); the
  successor's browser started after 1.5-2.0 s and 6.0-6.7 s. Five
  windows-latest runs, no overlap in either lane, so the daemon adds none.
  The watcher samples: a process that lives wholly between two samples is not
  seen. Source `2d6e0b99`, 2026-10-12. Test
  `tests/differential/test_crash_overlap_rows.py::test_no_successor_browser_overlaps_only_under_the_daemon`.
- **An inherited handle does not hold the lock on Windows.** A child that
  inherited the handle did not hold the lock once the parent had closed its
  handles and exited, in 20 of 20 runs. Justifies the owner taking the lock
  itself on every platform (`daemon.lock.DaemonLock`). Source `50dfdbf5`,
  2026-07-28.
- **A collected wrapper leaves the lock held.** After dropping the last
  reference to an acquired lock, the registry was empty and another process
  still could not take it. Justifies `daemon.lock._live_locks` holding a held
  lock strongly. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_lock.py::TestRegistry::test_a_held_lock_survives_losing_its_last_reference`.
- **An untouched fork child keeps the lock.** After the owner released and
  exited, a fork child that never touched the lock kept it held, so no other
  process could elect a replacement. Justifies
  `daemon.lock._discard_inherited_locks`. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_lock.py::TestFork::test_a_child_that_never_touches_the_lock_does_not_hold_it`.

## Descriptor and auth root

- **Containers sharing a mounted profile do not share daemon state.** Measured
  with two containers on one mounted profile: both saw the same auth root
  identity and both took the lock, because each has its own home. Applies to
  `daemon.descriptor.daemon_state_root`. Source `50dfdbf5`, 2026-07-28.
- **A path changed under a running owner splits the lock.** After retargeting a
  link or renaming a directory, a second process took a lock on a different
  inode while the first still held its own. Applies to
  `daemon.descriptor.daemon_state_root`. Source `50dfdbf5`, 2026-07-28.
- **A non-ASCII digest raised outside `DescriptorError`.** `hmac.compare_digest`
  raises `TypeError` for a string with a non-ASCII character. A descriptor with
  an accented character was accepted and the comparison failed later with that
  `TypeError`. Justifies `daemon.descriptor._digest_text`. Source `50dfdbf5`,
  2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestDigestFields::test_a_field_that_is_not_a_digest_is_refused`.
- **A file at the auth root surfaced as `NotADirectoryError`.** With the check
  after `secure_mkdir`, a file where the auth root belongs surfaced as
  `NotADirectoryError`. Justifies checking before creating in
  `daemon.descriptor._auth_root_identity`. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestAuthRootShape::test_a_file_where_the_auth_root_belongs_is_refused`.
- **A vanished auth root surfaced as `FileNotFoundError`.** An auth root that
  vanished between creating it and reading it back surfaced as
  `FileNotFoundError`. Applies to `daemon.descriptor._auth_root_identity`.
  Source `50dfdbf5`, 2026-07-28.
- **The first login changed a profile's identity.** With the auth root
  non-empty, `serves()` went from True to False across the login that created
  the profile directory. Justifies `daemon.descriptor.profile_identity`
  consulting only the parent. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestProfileIdentityStability::test_the_identity_survives_the_profile_being_created`.
- **Case folding merged two profiles on case-sensitive APFS.** On a
  case-sensitive APFS volume, once with both `Profile` and `profile` present
  and once with only the sibling, two distinct profiles shared an identity,
  which would have served a client the other one's logged-in session. Justifies
  asking the filesystem in `daemon.descriptor.profile_identity`. Source
  `50dfdbf5`, 2026-07-28. Tests
  `tests/test_daemon_descriptor.py::TestProfileIdentityStability::test_case_distinct_siblings_do_not_collide`
  and
  `tests/test_daemon_descriptor.py::TestProfileIdentityStability::test_a_missing_profile_does_not_borrow_a_siblings_identity`.
- **Composition needs normalising.** On macOS an accented name written
  decomposed and composed is one directory, and comparing the two spellings
  without normalising gave them different identities. Justifies NFC in
  `daemon.descriptor.profile_identity`. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestProfileIdentityStability::test_an_alias_of_the_profile_name_itself_matches`.
- **The scan must not stop at the first fold match.** With `É` and `é` present
  and `é` asked for, the scan met `É` first, was told they differ, and gave up
  before reaching the entry that did match. Justifies continuing the scan in
  `daemon.descriptor.profile_identity`. Source `50dfdbf5`, 2026-07-28.
- **An unbracketed IPv6 literal made an unusable URL.** A valid `::1` endpoint
  produced a URL no client could use. Justifies bracketing in
  `daemon.descriptor.DaemonDescriptor.url`. Source `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestEndpointUrl::test_every_accepted_host_produces_a_usable_url`.
- **A denylist of control characters left thirty-six gaps.** An earlier version
  banned CR, LF, tab and NUL by name and left thirty-six other code points that
  were accepted here and rejected by the client. Justifies the printable-ASCII
  rule for the path in
  `daemon.descriptor.DaemonDescriptor.check_endpoint_is_local`. Source
  `50dfdbf5`, 2026-07-28. Test
  `tests/test_daemon_descriptor.py::TestEndpointSpelling::test_an_endpoint_that_composes_into_nothing_usable_is_refused`.
- **Every looser host rule let a wrong host through.** A loopback check plus a
  `urlsplit` round trip and the resolver accepted, before each was patched:
  `"[::1] "` and a bracketed IPv4, which failed later in the client; a
  space-padded address whose URL named `"%20127.0.0.1%20"`; `"127.0.0.1."` and
  `"localhost.."`, which `is_loopback_host` passed after stripping the dots and
  `getaddrinfo` refused; and `localhost` with the resolver made to answer
  `203.0.113.9`, so the token would have been posted off this machine. With a
  stalled resolver, `getaddrinfo` also took two seconds for a two second stall,
  and is unbounded in principle. Justifies accepting only the two literals an
  owner writes, `daemon.descriptor._ENDPOINT_HOSTS`, with no resolver. Source
  `50dfdbf5`, 2026-07-28. Tests
  `tests/test_daemon_descriptor.py::TestEndpointSpelling::test_an_endpoint_that_composes_into_nothing_usable_is_refused`
  and
  `tests/test_daemon_descriptor.py::TestEndpointSpelling::test_a_loopback_name_or_other_spelling_is_refused`.
- **The owner publishes `::1` or `127.0.0.1`, unbracketed.** It binds `::1`
  first and `127.0.0.1` when IPv6 is unavailable, and publishes the address
  the socket reports. The frozen baseline (`BASELINE_SHA` in
  `tests/differential/baseline.py`) does the same. Applies to
  `daemon.owner._endpoint_host`. Source `24e9bf9e`, read 2026-10-12. Test
  `tests/test_daemon_descriptor.py::TestEndpointSpelling::test_the_address_an_owner_binds_is_published_and_reaches_it`.

## Election and startup

- **A dead owner refuses fast; a stalled one spends the budget.** Against the
  real probe with a real descriptor and token, a closed port refused in about
  ten milliseconds, while a process holding a listening socket that nobody
  reads spent a full five seconds. Justifies `daemon.core.lookup.Reach` and
  `daemon.election._REACHABLE_SECONDS`. Source `a9ca8138`, 2026-07-31. Test
  `tests/test_daemon_election.py::TestSilenceIsNotDeath::test_a_dead_endpoint_and_a_frozen_one_are_told_apart`.
- **A boolean probe buried a live owner.** When the probe answered `bool`, a
  frontend buried a live owner it had just started because one ping landed in a
  scheduler stall. Justifies the three-valued `daemon.election._reachable`.
  Source `a9ca8138`, 2026-07-31. Test
  `tests/test_daemon_election.py::TestRealOwner::test_a_paused_owner_is_waited_for_rather_than_written_off`.
- **A ping fails against a healthy owner in the 2026-07-28 era.** That protocol
  era removed ping (SDK `docs/migration.md:1800`), a default client negotiates
  it with a current owner, and a ping came back as an error from a healthy
  owner. Justifies the tool listing in `daemon.election._reachable` and
  `daemon.owner.prove_hop`. Source `b3fcac2e`, 2026-09-27. Test
  `tests/test_daemon_election.py::TestSilenceIsNotDeath::test_a_healthy_owner_answers`.
- **A default-negotiated probe selected an owner no call could reach.** With
  FastMCP 4.0.10 and MCP 2.2.0, an owner of this package version that refused
  `2026-07-28` at `server/discover` and served `2025-11-25` passed the probe
  through the SDK's legacy fallback and was selected in 62 ms. Every forwarded
  call then failed at the pinned `server/discover`, its recovery selected the
  same generation, and each call ended as a lost owner in 15 to 30 ms with no
  tool request sent. Justifies `daemon.election._reachable` negotiating through
  `daemon.forwarder.pin_hop_version`. Source `b18083a1`, 2026-10-14. Test
  `tests/test_daemon_election.py::TestTheProbeSpeaksTheHopsVersion::test_an_owner_without_the_pinned_version_is_not_attached_to`.
- **A flat poll interval overran a small budget.** Before the clamp, a flat
  poll interval turned a 1 ms budget into 101 ms. Justifies never pausing past
  the wait in `daemon.core.lookup`. Source `98e99338`, 2026-07-28. Test
  `tests/test_daemon_election_core.py::TestOneLook::test_a_small_wait_is_not_rounded_up_to_a_poll`.
- **A replacement is read through a dead owner's file.** An owner that crashed
  serving a sibling profile reads as `INCOMPATIBLE` while its replacement is
  coming up. Justifies `daemon.core.lookup` waiting through an unusable
  predecessor descriptor for its replacement. The control-only readings in
  `daemon.core.lookup.wants_a_decision` are decided at once instead: another
  protocol, or an older owner with another configuration, is asked to stand
  down or written off, and a same-version owner with another configuration is
  probed once and, answering or silent, ends as `LIVE_RIVAL` or
  `SILENT_RIVAL`. Source `98e99338`, 2026-07-28. Test
  `tests/test_daemon_election_core.py::TestOneLook::test_a_replacement_is_seen_through_an_unusable_reading`.
- **A boolean start result cost a minute.** A child that reported it could not
  serve read as "somebody is starting", so the caller waited out its whole
  deadline for a descriptor nothing was going to write. Justifies
  `daemon.core.spawn.Attempt`. Source `6d369701`, 2026-07-29.
- **No zombie without `_reap`.** A failed election left no zombie with the
  `_reap` call removed, because `subprocess` reaps abandoned children when the
  next one is created. `daemon.spawn._reap` stays because that is a
  standard-library detail. Source `6d369701`, 2026-07-29.
- **Closing a parked reader blocks.** `BufferedReader.close()` blocked for 29.27
  of thirty seconds against a silent child. Justifies detaching first in
  `daemon.startup._close_output`. Source `6d369701`, 2026-07-29. Test
  `tests/test_daemon_election.py::TestFailingFast::test_a_child_that_says_nothing_does_not_keep_the_lock`.
- **`start_new_session` does nothing on Windows.** CPython's Windows
  implementation names the parameter `unused_start_new_session` and ignores it
  (`subprocess.py:1461`). Justifies the creation flags in
  `daemon.spawn._detachment_flags`. Source `6d369701`, 2026-07-29.
- **The election reducers decide what the shell decided before.** Run against
  the code they replaced, the extracted reducers matched its results, ordered
  effects and scripted timing on 1,224 lookup cases, 2,500 election traces and
  126 startup cases. Justifies moving `daemon.core.lookup`,
  `daemon.core.spawn` and `daemon.core.election` out of `daemon.election`
  without a behaviour change. Source review of `8bde6775` against `f4073e29`,
  merged as `45d54e18`, 2026-10-12.
- **Darwin's killpg answers EPERM before waitid sees the kill.** On Darwin
  27.0.0 under load, after a first `SIGKILL` to a worker group, the next
  `killpg` raised EPERM while the leader was in state `E` or already a zombie,
  and `waitid` reported its exit 0 to 47 ms later. XNU's `killpg1` skips
  zombies in a group and answers EPERM when it found nothing to signal
  (`bsd/kern/kern_sig.c`). Read as a group nobody can signal, it made the
  installer supervisor exit 70 after a finished install in 44 of 200 loaded
  runs. Justifies waiting out EPERM once a kill has landed in
  `process_tree.terminate_process_group`, through which both the installer
  supervisor and `daemon.spawn` stop their children. Source PR #1302,
  2026-10-12. Tests
  `tests/test_process_tree.py::TestPosixProcessGroups::test_eperm_before_the_killed_leader_is_waitable_is_waited_out`
  and
  `tests/test_installer_supervisor.py::test_a_startup_hook_cannot_spawn_a_pipe_holder_in_the_supervisor`.

## Owner process and HTTP

- **The owner survives its frontend on POSIX.** After `SIGKILL` to the
  frontend's whole process group, the owner was still running and had
  reparented to pid 1. On Windows `start_new_session` is POSIX-only
  (`subprocess.py:795`). Applies to the `daemon.owner` docstring and
  `daemon.spawn._detachment_flags`. Source `6d369701`, 2026-07-29. Test
  `tests/test_daemon_election.py::TestRealOwner::test_the_owner_outlives_the_client_that_started_it`.
- **In a container the owner dies with the display.** Inside the image the
  owner had a distinct process group and was still alive when EOF ended the
  frontend, then the container's PID namespace killed it with Xvfb. Justifies
  refusing the daemon in a container in `daemon.discovery._daemon_would_be_used`.
  Source `8d5c3bd0`, 2026-08-09. Test
  `tests/test_daemon.py::TestWhetherTheDaemonAppliesAtAll::test_a_container_refuses_the_daemon_and_says_why`.
- **An environment proxy received the bearer token.** httpx2 honours
  `HTTP_PROXY` even for `127.0.0.1` unless `NO_PROXY` says otherwise.
  Reproduced against a capture proxy: a request to `http://127.0.0.1:9/...`
  arrived at the proxy with `Authorization: Bearer <token>`. Justifies
  `trust_env=False` in `daemon.owner.direct_http_client` and
  `daemon.owner.direct_async_http_client`. Source `6d369701`, 2026-07-29. Tests
  `tests/test_daemon_election.py::TestVersionSkew::test_the_token_never_goes_through_a_configured_proxy`
  and
  `tests/test_daemon_proxy.py::TestKeepingTheTokenOffTheNetwork::test_the_environment_proxy_is_refused`.
- **FastMCP passes more than the factory protocol.** FastMCP's transport passes
  `follow_redirects` on top of the `McpHttpClientFactory` protocol
  (`fastmcp/client/transports/http.py:176-181`). A factory that accepted only
  the documented signature failed at connect time with an unexpected keyword,
  and took every real-owner test with it. Justifies `**extra` in
  `daemon.owner.direct_async_http_client`. Source `6d369701`, 2026-07-29.
- **A non-ASCII token turned a 401 into a 500.** Compared directly as strings, a
  header of `Bearer töken` raised `TypeError` inside the route and the caller
  got a 500 where it should get a 401. Justifies comparing digests in
  `daemon.owner._matches_token`. Source `6d369701`, 2026-07-29. Test
  `tests/test_daemon_election.py::TestVersionSkew::test_a_non_ascii_token_is_refused_rather_than_raising`.
- **Uvicorn driven directly still runs the lifespan.** Verified against the
  installed FastMCP 3.4.4: `run_http_async` builds its own server, binds its
  own port and blocks with no handle to stop it
  (`fastmcp/server/mixins/transport.py`), and a lifespan still runs both ways
  round when uvicorn is driven directly. Justifies serving through uvicorn
  in `daemon.owner.create_owner_server`. Source `6d369701`, 2026-07-29.
- **Custom routes sit outside the auth middleware.** Measured on FastMCP 3.4.4:
  a custom route is mounted outside the authentication middleware, so an
  unauthenticated POST to the stand-down path was served. Justifies the
  explicit token checks in the stand-down and heartbeat routes of
  `daemon.owner.create_owner_server`. Source `6d369701`, 2026-07-29. Tests
  `tests/test_daemon_election.py::TestVersionSkew::test_a_stand_down_request_needs_the_token`
  and
  `tests/test_daemon_liveness.py::TestTheRouteOnARealOwner::test_a_beat_without_the_token_is_refused_and_does_not_count`.
- **Ending the wait did not end the process.** Before the hard exit existed,
  the helper returned on time and the process never came out of `asyncio.run`.
  Justifies the hard exit in `daemon.owner._stop_within`. Source `6d369701`,
  2026-07-29. Test
  `tests/test_daemon_election.py::TestNotHoldingTheLockForever::test_a_shutdown_that_resists_cancellation_still_ends_the_process`.

## Proxy and forwarding

- **A stand-down left attached proxies dead.** Reproduced end to end: a proxy
  serving 19 tools, whose owner was told to stand down the way a newer build
  does (`daemon.election._ask_to_stand_down`), failed its next listing with
  `McpError: Client failed to connect`. Justifies recovery in
  `daemon.proxy.DaemonProxyBackend`. Source `619ff243`, 2026-07-30. Test
  `tests/test_daemon_proxy.py::TestRecoveringThroughTheWholeProxy::test_a_client_lists_again_after_its_owner_went_away`.
- **A tool's timeout starts after the queue.** A tool declared with a one
  second timeout, queued two seconds behind another call, succeeded after
  2.02 s. So no margin derived from the tool timeout covers a queued call.
  Applies to `daemon.proxy._TIMEOUT_MARGIN_SECONDS`. Source `619ff243`,
  2026-07-30.
- **Cancelling a `to_thread` awaiter does not stop the worker.** Driven
  directly, an awaiter cancelled at 0.1 s while its worker still finished its
  1.5 s of work. Justifies shielding the election in
  `daemon.proxy.DaemonProxyBackend.recover`. Source `c13b974e`, 2026-08-01. Test
  `tests/test_daemon_proxy.py::TestServingTheOwnersTools::test_a_caller_that_gives_up_does_not_free_the_election`.
- **The configuration singleton gave the wrong budget.** On a server built
  directly, a 1.2-second call budgeted itself 149.8 seconds from the
  singleton's default. Justifies `daemon.proxy.FrontendCallMiddleware` taking
  the tool timeout as an argument. Source `52370602`, 2026-07-31.
- **Only `MCPError` reaches the wire intact.** On a 2026-07-28 connection any
  other exception becomes `Internal server error` (`mcp/server/runner.py`,
  `modern_error_data`, SDK 2.2.0). Justifies
  `daemon.proxy._owner_lost_from_a_listing` returning an `MCPError`. Source
  `3da596de`, 2026-10-05.
- **Masking hides a re-raised owner loss.** Measured against fastmcp 3.4.7
  with `mask_error_details=True`: re-raising `OwnerUnreachableError` left the
  client only `ToolError("Error calling tool 'send_message'")`
  (`fastmcp/server/server.py:1342-1358`), while a `ToolError` raised in the
  middleware kept its own text (`:1327-1331`). Neither carries `status` and
  `retry_safe`, which is why
  `daemon.proxy.FrontendCallMiddleware._report_an_unknown_outcome` returns a
  result. Source `824c0f46`, 2026-09-18. Test
  `tests/test_daemon_proxy.py::TestRecoveringThroughTheWholeProxy::test_a_mutating_call_whose_answer_never_came_is_not_repeated`.
- **An error result skips output-schema validation.** An error result travels
  as a whole `CallToolResult`, which `mcp.server.lowlevel.server` returns
  unchanged instead of validating it against the called tool's output schema.
  Applies to
  `daemon.proxy.FrontendCallMiddleware._report_an_unknown_outcome`. Source
  `824c0f46`, 2026-09-18.

## Liveness and heartbeats

- **Cancellation does not cross the hop.** Over a real loopback owner, the
  client was answered at 0.66 s with an error and the effect landed 0.7 s
  later. Justifies `daemon.liveness` and the `COULD_CHANGE` refusal in
  `daemon.auth_repair.say_why`. Source `52370602`, 2026-07-31. Tests
  `tests/test_daemon_auth.py::TestTheRepairRunsForReal::test_a_tool_that_changes_something_is_never_replayed`
  and
  `tests/test_daemon_proxy.py::TestRepeatingOnlyWhatIsSafe::test_a_mutating_call_is_not_repeated_when_it_may_have_run`.
- **A heartbeat is cheap.** Against a server built the way the owner builds
  one, a heartbeat round trip cost 0.9 ms median while the owner was idle and
  1.8 ms while it was driving Chromium, so the cadence occupies an owner about a
  thousandth of its time per waiting call. Justifies
  `daemon.liveness.HEARTBEAT_SECONDS`. Source `d69bcc8c`, 2026-08-01.
- **The expiry margin is wide.** Worst cases: 9.5 ms for a heartbeat served
  while a page is being driven, 3.6 ms of timer lateness in a detached process
  over 25 beats, and 130 ms for the longest the owner's synchronous text
  extraction holds its event loop, on a 2 MiB page far larger than a real one.
  A live call needs a stall roughly seventy times worse than anything measured,
  or three consecutive beats lost, before it is wrongly cancelled. Justifies
  `daemon.core.owner.EXPIRY_SECONDS`. Source `d69bcc8c`, 2026-08-01.
- **A stopped owner is silent, not gone.** On macOS 27.0.1, a SIGSTOPped real
  owner kept accepting connections, and every heartbeat to it ended in
  `httpx2.ReadTimeout` 1.002 to 1.005 s into a 1 s budget, both before a call
  was dispatched and while one was in flight. The election's probe found it
  `SILENT`, and after SIGCONT the same pid answered within 6 ms and served the
  call. An owner whose event loop was blocked, rather than stopped, timed out
  the same way at 1.003 s. Three runs each. Justifies
  `daemon.discovery.Unanswered` and counting only `ReadTimeout` as silence in
  `daemon.proxy.Responsiveness`. Source #1295 on `3d458d82`, 2026-10-12. Tests
  `tests/test_daemon_silence.py::TestAStoppedRealOwner::test_stopped_before_the_call`,
  `tests/test_daemon_silence.py::TestAStoppedRealOwner::test_stopped_after_the_request_left`
  and
  `tests/test_daemon_silence.py::TestABlockedOwnerLoop::test_a_blocked_owner_is_named_and_sent_nothing`.

## Auth repair

- **An exception loses the marker; a result keeps it.** Measured against
  fastmcp 3.4.4: a middleware that re-raised delivered
  `meta=None, isError=True, content=["Error calling tool 'x'"]` to the outer
  client, because `mask_error_details` is on. Returning
  `ToolResult(..., meta=..., is_error=True)` delivered the marker intact, and a
  client that raises on error still got its `ToolError` with the original
  message. The proxy copies `meta` and `isError` explicitly
  (`fastmcp/server/providers/proxy.py:212-225`). Justifies the result-shaped
  refusal in `daemon.auth_repair`. Source `52370602`, 2026-07-31. Test
  `tests/test_daemon_auth.py::TestTheMarkerSurvivesTheHop::test_the_marker_reaches_the_outer_client_through_the_proxy`.
- **The cause chain once arrived severed.** Before `raise_tool_error` passed an
  already-shaped `ToolError` through, the missing-session path arrived as
  `['ToolError']` with the cause severed. Applies to the `__cause__` walk in
  `daemon.auth_repair`. Source `52370602`, 2026-07-31. Test
  `tests/test_daemon_auth.py::TestTheMarkerSurvivesTheHop::test_the_marker_is_found_through_the_wrapping_tool_error`.
- **Every origin looks the same to a client.** Counted in the source: all 18
  tool catch sites collapse failures into one `except` clause, so only the owner
  can decide `replayable`. Source `52370602`, 2026-07-31.
- **The marker key sits beside fastmcp's own.** A custom meta key coexists with
  fastmcp's own `{'fastmcp': {...}}` entry as a separate top-level key. Applies
  to `daemon.auth_repair.MARKER_KEY`. Source `52370602`, 2026-07-31.
- **A started login is not a failed repair.** Read as a failure, a started
  login abandoned every repair: on the stale path with a login that finished
  successfully, the owner was called once and the client still got the error.
  The missing path did the same as soon as signing in took longer than the
  inline budget. Justifies `daemon.auth_repair.start_sign_in`. Source
  `52370602`, 2026-07-31. Tests
  `tests/test_daemon_auth.py::TestTheRepairRunsForReal::test_a_stale_session_is_replayed_once_the_login_succeeds`
  and
  `tests/test_daemon_auth.py::TestTheRepairRunsForReal::test_a_slow_sign_in_is_still_replayed`.
- **A client-cancelled replay still sends.** Beyond the 0.66 s and 0.7 s
  observation under liveness, an MCP client that gave up on its own call
  produced the same orphan, measured as effects `['sent']` after the cancel and
  `['sent', 'sent']` after the retry. Justifies `COULD_CHANGE` in
  `daemon.auth_repair.say_why`. Source `52370602`, 2026-07-31. Test
  `tests/test_daemon_auth.py::TestTheRepairRunsForReal::test_a_tool_that_changes_something_is_never_replayed`.

## Release gates

- **Gate 1 passes on `f39a2893`.** One dispatch of
  `.github/workflows/differential-evidence.yml` with `platform=all` and
  `group=all`, run 38613606587, attempt 1, 38 of 38 jobs successful, reviewed
  cell by cell against the contract and both Gate 1 decision records. This
  settles Gate 1 only, not gates 2 to 4. Source run 38613606587 on
  `f39a28930e54deab30394f04786fa9821344c306`, 2026-10-14.
  - Candidate and harness revision `f39a2893` (`tests/differential` tree
    `7a5bdf5a`). `uv.lock` sha256 `26d7c58c3fbed8ea259402c0eaee8c75ecd10ae2f426fc1c6b5513967df4dc24`;
    Windows hashes the same file to `38dd5e4b...` after its CRLF checkout, the
    same content. Frozen Direct pin `0253421539fffd4c9b207ca62b6efb41a8905ed3`.
  - Executed and skipped cases per experiment, failures zero throughout:

    | OS / architecture | K0 | K1 | K2 | K3 |
    |---|---:|---:|---:|---:|
    | Linux x86_64 | 41 / 1 | 36 / 8 | 7 / 0 | 150 / 3 |
    | Linux aarch64 | 41 / 1 | 36 / 8 | 7 / 0 | 150 / 3 |
    | macOS arm64 | 40 / 2 | 34 / 10 | 4 / 3 | 148 / 5 |
    | Windows amd64 | 35 / 9 | 30 / 16 | 5 / 3 | 143 / 12 |

    907 executed cases, 471 native and 436 source-model; 509 executed cells
    of 1,504 combinations, the rest skipped (70) or inapplicable. No required
    cell is zero.
  - 159 K0 repeat pairs with no difference, and 148 Direct comparisons with no
    O1, O3 or O4 difference. 60 required Linux traced O2 scopes, 26 `held` and
    34 `unknown`, none violated. The nine O4 `lost-silent` labels are
    H-R10a-login's authorized replacements, each matching Direct.
  - Every skip is one the decision records allow or a structural
    inapplicability the contract names: H-R7 on macOS (11), Windows
    terminal-bound profile commands (12), idle-owner import (4), H-R12-http on
    Windows (3), the native H-R16 lane (4, its source model runs), and 50 cases
    with no counterpart on that platform or in Direct.
  - Every K2 witness reproduced at the pin: H-R12 W-CHROME-PATH on all four
    platforms, H-R6 owner group natively on both Linux architectures, H-R7 owner
    sweep three times per Linux architecture, H-R11 on Windows, and
    W-UNMARKED and W-OWNER-UNMARKED on all four. The H-R16 response-loss
    witness passed both cuts on every platform, 8 of 8.
  - One staging relaunch, Windows H-R15 K0: the first browser exhausted the
    30 s navigation budget before a session was validated, was gone before the
    second launch, and the row then matched K3. Its cause is not established.
    No job reran; no census entry stayed unresolved.
  - Runner images: ubuntu-24.04 `20261004.327.1` (8 jobs) and
    `20261012.334.1` (2); ubuntu-24.04-arm `20261004.142.1` (4) and
    `20261012.149.1` (5); macos-26-arm64 `20260907.0351.1` (9);
    windows-2025-vs2026 `20260925.250.1` (9) and `20261007.264.1` (1).
    Candidate browser `153.0.8010.12` (patchright 1.63.0) everywhere; frozen
    browser `149.0.7827.55` (patchright 1.61.2), `149.0.7827.0` on Linux
    aarch64.
  - All 37 artifacts were archived before their 2026-10-28 expiry, and the
    maintainer keeps that archive outside the repository; every zip matched
    its API digest. The checksum list over the 4,599 extracted files has sha256
    `c3a53d56588df04614e83d8288b2d7095a72999b224321180837f485b30e796e`, so a
    copy obtained from the maintainer can be checked against this entry.
