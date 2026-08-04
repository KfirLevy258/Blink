# Code Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the fifteen defects the 2026-08-04 review found in the six commits dated 2026-07-27 (display SPI, OAuth transport retry, UI transitions, settings chevron, clip player, prj.conf).

**Architecture:** Eight independent changes plus a hardware pass, each change touching one file or one subsystem, ordered so the two that both rewrite `net_worker` land in sequence. The OAuth aliasing fix is extracted into a pure snapshot/retain pair so a host test can actually reach it — today's assertions cannot, because the code that produces `-EACCES` is compiled out of the host build. Everything else is verified by building and by a scripted hardware pass at the end, because this firmware has no on-target test harness.

**Tech Stack:** Zephyr 4.4 / ESP32 (CYD board, `esp32_devkitc/esp32/procpu`), LVGL 9.3, mbedTLS, MCUboot + sysbuild, flash encryption. Host tests are plain C compiled with `cc`.

## Global Constraints

- **Commits must NOT carry a `Co-Authored-By: Claude` trailer.** This repo pushes to the public `KfirLevy258/Clauge`.
- **Every on-screen sentence starts with a capital letter** (sentence case).
- **"Done" means flashed to the board and boot-verified, not merely built.** Task 9 is not optional.
- **Build is sysbuild + MCUboot only**, into `build-sb`, board `esp32_devkitc/esp32/procpu`. A different `-b` poisons the build dir; recover with `-p always`.
- **Flash only via `tools/flash_encrypted.sh`.** A plain `west flash` writes plaintext the encrypted ROM cannot boot and leaves the board dark.
- Opening the serial port resets the board, and the port name varies by USB socket. Stop any logger before flashing.

**The build command, used verbatim in every task that builds:**

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
cd ~/Projects/LiveClaudeUi/firmware
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.clauge/ota_signing_key_p256.pem\""
```

---

### Task 1: Stop the SPI driver returning before the transfer finishes

`CONFIG_SPI_ESP32_INTERRUPT=y` was added on 2026-07-27 to "sleep on the transfer-complete interrupt instead of busy-polling". The driver does not implement that. In interrupt mode `transceive()` arms the interrupt and falls straight through to `spi_context_release()`; `grep spi_context_wait_for_completion` over `drivers/spi/spi_esp32_spim.c` returns nothing, and `CONFIG_SPI_ASYNC` is off, so the `spi_context_complete()` the ISR calls gives a semaphore nobody takes. Every synchronous `spi_transceive()` became fire-and-forget.

Two things break. The Kconfig is driver-wide (`if ESP32_SPIM`), so spi3/XPT2046 gets it too, and `input_xpt2046.c` reads its RX buffer on the line after `spi_transceive_dt()` returns — 16 averaged reads per report, every one of them potentially stale. That is the likeliest cause of the "not really clickable" report that the chevron change in Task 7 was written to work around. And `display_write()` now returns with DMA still reading, so `ui_anim.c` hands its 4096-byte strip back to the LVGL pool mid-transfer.

The busy-poll the comment wanted to remove is still there either way — `while (!spi_hal_usr_is_done(hal))` at `spi_esp32_spim.c:229` is unchanged, merely relocated into the ISR.

Separately, that ISR is registered with `ESP_INTR_FLAG_IRAM` but reaches `gpio_esp32_port_clear_bits_raw`, which this build places in `.flash.text`. Any NVS or OTA write disables the flash cache while leaving IRAM-flagged interrupts live, so a completion firing in that window executes an unmapped address. Turning the option off retires that too — no ISR is registered at all.

**Files:**
- Modify: `firmware/prj.conf:36-40`

- [ ] **Step 1: Confirm the driver really has no completion wait**

```bash
grep -c spi_context_wait_for_completion ~/zephyrproject/zephyr/drivers/spi/spi_esp32_spim.c
grep -n "CONFIG_SPI_ASYNC" ~/Projects/LiveClaudeUi/firmware/build-sb/firmware/zephyr/.config
```

Expected: `0`, and `# CONFIG_SPI_ASYNC is not set`. If either differs, stop and re-read the driver before changing anything.

- [ ] **Step 2: Turn the option off, with the evidence in the comment**

Replace `firmware/prj.conf:36-40` (the five lines from `# Sleep on the transfer-complete interrupt` through `CONFIG_SPI_ESP32_INTERRUPT=y`) with:

```
# Explicitly OFF, which is also the Kconfig default -- kept as a line rather
# than a deletion so nobody re-adds it for the reason we did.
#
# It was set on 2026-07-27 to "sleep on the transfer-complete interrupt instead
# of busy-polling". The driver does not do that. With it on, transceive() arms
# the interrupt and falls straight through to spi_context_release() --
# spi_esp32_spim.c has no spi_context_wait_for_completion() call, and
# CONFIG_SPI_ASYNC is off, so the spi_context_complete() the ISR performs only
# gives a semaphore nobody takes. Every synchronous transfer became
# fire-and-forget: XPT2046 read its RX buffer before the words landed (16
# averaged reads per touch report), and display_write() returned with DMA still
# reading the caller's strip.
#
# The busy-poll it was meant to remove survives either way -- the
# `while (!spi_hal_usr_is_done(hal))` spin is still there, just moved into the
# ISR. And that ISR is registered ESP_INTR_FLAG_IRAM while its CS teardown
# reaches gpio_esp32_port_clear_bits_raw in .flash.text, so it would fault if it
# fired during an NVS or OTA write, when the flash cache is down.
CONFIG_SPI_ESP32_INTERRUPT=n
```

- [ ] **Step 3: Build**

Run the build command from Global Constraints.
Expected: links clean. Confirm the option took:

```bash
grep -n "SPI_ESP32_INTERRUPT" ~/Projects/LiveClaudeUi/firmware/build-sb/firmware/zephyr/.config
```

Expected: `# CONFIG_SPI_ESP32_INTERRUPT is not set`.

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/prj.conf
git commit -m "SPI: stop transceive returning before the transfer finishes

CONFIG_SPI_ESP32_INTERRUPT has no completion wait in this driver, so every
synchronous transfer became fire-and-forget: touch read stale RX words and
display_write returned with DMA still reading the caller's buffer."
```

---

### Task 2: Keep the refresh token across a refresh that does not rotate it

`main.c` calls `oauth_refresh(tok.refresh, &tok)` — the input argument *is* the output field. `token_post()` parses the reply straight into `*out`, and when Anthropic omits `refresh_token` (a case `oauth.c:251` explicitly anticipates), `oauth.c:253` blanks the very buffer the input pointer aims at. The fallback at `oauth.c:306` then copies the now-empty string onto itself, returns 0, and the caller persists an empty token. `cfg_store`'s `if (!cfg.token[0]) return false` makes that indistinguishable from having no token, so the next boot drops into provisioning — the exact permanent logout the 2026-07-27 change was written to prevent.

The fix is to snapshot before the call. Splitting that into a snapshot/retain pair is what makes it testable: `oauth_refresh` itself lives inside `#ifndef OAUTH_HOST_TEST` and is not linked into the host binary, which is also why the six `-EACCES` assertions added on 2026-07-27 are a tautology — `nm` on the test binary shows `oauth_creds_rejected` as the only OAuth symbol, so changing `oauth.c:232` to `-EPERM` breaks the device without failing a single assertion. The new pair lives outside the guard, so the host test drives the real code the device runs.

**Files:**
- Modify: `firmware/src/oauth.h:46` (add two declarations after `oauth_refresh`)
- Modify: `firmware/src/oauth.c:120-125` (add the pair beside `oauth_creds_rejected`), `firmware/src/oauth.c:294-311` (`oauth_refresh`)
- Test: `tests/oauth/host_test.c`

**Interfaces:**
- Produces: `void oauth_refresh_snapshot(const char *refresh_token, char *keep, size_t keeplen)` and `void oauth_refresh_retain(const char *keep, struct oauth_tokens *out)`. Task 4 relies on `oauth_refresh()` never returning 0 with an empty `out->refresh`.

- [ ] **Step 1: Write the failing test**

Append to `tests/oauth/host_test.c`, immediately before the `printf("\n%s (%d failures)\n", ...)` line:

```c
	/*
	 * The stored refresh token must survive a refresh that returns no new
	 * one -- including under the aliasing every caller uses.
	 *
	 * main.c calls oauth_refresh(tok.refresh, &tok): the argument IS the
	 * output field, and token_post() blanks that field before the fallback
	 * runs. Snapshotting before the call is the whole fix; these two
	 * functions are the seam that lets a host test reach it.
	 */
	{
		struct oauth_tokens tok;
		char keep[OAUTH_TOKEN_LEN];

		memset(&tok, 0, sizeof(tok));
		strcpy(tok.refresh, "OLD-REFRESH");

		oauth_refresh_snapshot(tok.refresh, keep, sizeof(keep));
		tok.refresh[0] = '\0';	/* what token_post does on a reply with no refresh_token */
		oauth_refresh_retain(keep, &tok);
		CK(strcmp(tok.refresh, "OLD-REFRESH") == 0,
		   "omitted refresh_token keeps the stored one (aliased caller)");

		strcpy(tok.refresh, "NEW-REFRESH");
		oauth_refresh_retain(keep, &tok);
		CK(strcmp(tok.refresh, "NEW-REFRESH") == 0,
		   "rotated refresh_token is kept, not overwritten");

		oauth_refresh_snapshot("", keep, sizeof(keep));
		CK(keep[0] == '\0', "empty snapshot stays empty");
	}
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd ~/Projects/LiveClaudeUi
cc -DOAUTH_HOST_TEST -I firmware/src tests/oauth/host_test.c firmware/src/oauth.c -o /tmp/oa
```

Expected: FAIL to compile — `implicit declaration of function 'oauth_refresh_snapshot'` and an undefined symbol at link.

- [ ] **Step 3: Declare the pair in the header**

In `firmware/src/oauth.h`, insert after the `oauth_refresh` declaration (line 46) and before the `oauth_creds_rejected` comment block:

```c
/*
 * Snapshot / restore pair for the refresh token across a token call. Pure, and
 * host-tested for exactly the aliasing below.
 *
 * Callers pass out->refresh AS the refresh_token argument -- main.c does it at
 * three sites -- and the endpoint's reply is parsed straight into *out. So by
 * the time we know whether a new refresh token came back, the old one is
 * already gone. Take the snapshot BEFORE the call; hand it back after.
 *
 * oauth_refresh() uses these itself; they are exposed so a host test can reach
 * the logic, which the TLS path around it cannot be.
 */
void oauth_refresh_snapshot(const char *refresh_token, char *keep, size_t keeplen);
void oauth_refresh_retain(const char *keep, struct oauth_tokens *out);
```

- [ ] **Step 4: Implement the pair**

In `firmware/src/oauth.c`, insert directly after `oauth_creds_rejected` (after line 125, before `#ifndef OAUTH_HOST_TEST`):

```c
/* See oauth.h. Outside the OAUTH_HOST_TEST guard on purpose: pure, and the
 * invariant it carries is what a permanent logout hinges on. */
void oauth_refresh_snapshot(const char *refresh_token, char *keep, size_t keeplen)
{
	if (keeplen == 0) {
		return;
	}
	strncpy(keep, refresh_token ? refresh_token : "", keeplen - 1);
	keep[keeplen - 1] = '\0';
}

void oauth_refresh_retain(const char *keep, struct oauth_tokens *out)
{
	if (out->refresh[0] != '\0') {
		return;		/* the endpoint rotated it -- keep the new one */
	}
	strncpy(out->refresh, keep, sizeof(out->refresh) - 1);
	out->refresh[sizeof(out->refresh) - 1] = '\0';
}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd ~/Projects/LiveClaudeUi
cc -DOAUTH_HOST_TEST -I firmware/src tests/oauth/host_test.c firmware/src/oauth.c -o /tmp/oa && /tmp/oa
```

Expected: `ALL PASSED (0 failures)`, 21 assertions.

- [ ] **Step 6: Use the pair in oauth_refresh**

Replace `firmware/src/oauth.c:294-311` (the whole `oauth_refresh` function) with:

```c
int oauth_refresh(const char *refresh_token, struct oauth_tokens *out)
{
	static char json[512];
	char keep[OAUTH_TOKEN_LEN];

	/* BEFORE token_post, not after: callers pass out->refresh in here, and
	 * token_post parses the reply straight into *out. A reply that omits
	 * refresh_token blanks the argument we were about to fall back on. */
	oauth_refresh_snapshot(refresh_token, keep, sizeof(keep));

	snprintf(json, sizeof(json),
		"{\"grant_type\":\"refresh_token\",\"refresh_token\":\"%s\","
		"\"client_id\":\"%s\"}",
		keep, CLIENT_ID);

	int rc = token_post(json, out);

	if (rc == 0) {
		oauth_refresh_retain(keep, out);
	}
	return rc;
}
```

- [ ] **Step 7: Re-run the host test and build the firmware**

```bash
cd ~/Projects/LiveClaudeUi
cc -DOAUTH_HOST_TEST -I firmware/src tests/oauth/host_test.c firmware/src/oauth.c -o /tmp/oa && /tmp/oa
```

Expected: still `ALL PASSED`. Then run the build command from Global Constraints. Expected: links clean.

- [ ] **Step 8: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/oauth.c firmware/src/oauth.h tests/oauth/host_test.c
git commit -m "OAuth: snapshot the refresh token before the call that overwrites it

Callers pass out->refresh as the input, and a reply omitting refresh_token
blanked it before the fallback could read it -- so a successful refresh
persisted an empty token and the next boot re-provisioned. The snapshot pair
lives outside the host-test guard so the case is actually covered."
```

---

### Task 3: One worker refresh path that backs off and re-provisions

Three sites in `net_worker` call `oauth_refresh` and each gets it wrong differently. `main.c:746` (proactive, 5 min before expiry) never advances `token_deadline` on failure, so once the window opens and a refresh fails, the condition stays true and re-fires on every 250 ms tick — back-to-back DNS + TLS + ECDHE at 1-2 s each, against the same 86 KB heap whose starvation caused the fatal `z_swap` crash. `main.c:784` (mid-run 401) discards the return entirely and never asks `oauth_creds_rejected()`, so a genuinely revoked token hot-loops two full handshakes every 5 s forever, with no re-provisioning path and no status posted. And `main.c:662` (boot) is the only one that classifies correctly.

Collapse all three onto one helper. Rejection is terminal and never returns; transport failure returns the errno and leaves the caller's tokens untouched so the retry has something valid to send.

**Files:**
- Modify: `firmware/src/main.c:10` (add `#include <errno.h>`), `firmware/src/main.c:634` (insert the helper above `net_worker`), `firmware/src/main.c:744-791` (the two in-loop call sites)

**Interfaces:**
- Consumes: `oauth_refresh()` from Task 2 — guaranteed never to return 0 with an empty `out->refresh`.
- Produces: `static int worker_refresh_token(const char *sent, struct oauth_tokens *tok, int64_t *deadline)`. Returns 0 with `*tok` holding a usable credential and `*deadline` reset; returns a negative errno on transport failure with `*tok` untouched; never returns on a rejected credential. Task 4 calls it for the boot refresh.

- [ ] **Step 1: Add the errno include**

In `firmware/src/main.c`, after line 10 (`#include <string.h>`):

```c
#include <errno.h>
```

- [ ] **Step 2: Add the helper above net_worker**

Insert into `firmware/src/main.c` immediately before `static void net_worker(...)` (line 635):

```c
/*
 * The worker's only way to refresh. Returns 0 with *tok holding a usable
 * credential and *deadline reset; returns a negative errno on TRANSPORT
 * failure, leaving *tok untouched so the caller still has something valid to
 * retry with. It does not return at all on a REJECTED credential: that is the
 * one failure a retry cannot fix, so it drops the token and reboots into
 * provisioning.
 *
 * The reply lands in a separate `fresh` rather than in *tok, so a caller
 * passing tok->refresh as `sent` (all of them do) is not reading a buffer this
 * function is writing.
 */
static int worker_refresh_token(const char *sent, struct oauth_tokens *tok,
				int64_t *deadline)
{
	struct oauth_tokens fresh;
	int rc = oauth_refresh(sent, &fresh);

	if (oauth_creds_rejected(rc)) {
		/* The "log in once" chain really is broken. Drop the token and
		 * reboot; with none stored the board re-provisions, keeping the
		 * WiFi credentials. */
		printk("[oauth] refresh rejected -- dropping the token, re-provisioning\n");
		cfg_clear_token();
		post_status(USAGE_STATUS_ERROR);
		k_sleep(K_SECONDS(3));
		ui_boot_mark_intentional_reboot();
		sys_reboot(SYS_REBOOT_COLD);
	}
	if (rc != 0) {
		return rc;
	}
	if (fresh.refresh[0] == '\0') {
		/* oauth_refresh guarantees this cannot happen. Belt and braces:
		 * persisting it would look exactly like having no token at all,
		 * and the next boot would re-provision. */
		printk("[oauth] refresh returned an empty token -- keeping the stored one\n");
		return -EINVAL;
	}
	*tok = fresh;
	cfg_set_token(tok->refresh);	/* persist a rotated token before use */
	*deadline = k_uptime_get() + (int64_t)tok->expires_in * 1000;
	return 0;
}
```

- [ ] **Step 3: Route the proactive refresh through it, with a backoff**

Replace `firmware/src/main.c:744-750` (`/* Refresh proactively, 5 min before expiry. */` through its closing brace) with:

```c
		/* Refresh proactively, 5 min before expiry. Backed off on
		 * failure: without a next-attempt stamp this condition stays
		 * true once the window opens, and a failing refresh re-fired a
		 * full DNS + TLS + ECDHE round on every 250 ms tick of this
		 * loop -- on the single core LVGL shares, against the heap whose
		 * starvation once crashed the board in z_swap. */
		if (now > token_deadline - 5 * 60 * 1000 && now >= next_refresh) {
			if (worker_refresh_token(tok.refresh, &tok,
						 &token_deadline) == 0) {
				refresh_wait_ms = REFRESH_RETRY_MIN_MS;
			} else {
				next_refresh = now + refresh_wait_ms;
				printk("[oauth] proactive refresh failed -- retry in %d s\n",
				       refresh_wait_ms / 1000);
				refresh_wait_ms = MIN(refresh_wait_ms * 2,
						      REFRESH_RETRY_MAX_MS);
			}
		}
```

`next_refresh` and `refresh_wait_ms` are declared in Task 4, which rewrites this function's locals. If you are running this task standalone, add them beside `next_poll`:

```c
		int64_t next_refresh = 0;
		int refresh_wait_ms = REFRESH_RETRY_MIN_MS;
```

- [ ] **Step 4: Route the mid-run 401 through it**

Replace `firmware/src/main.c:779-787` (the `} else if (r == USAGE_UNAUTHORIZED) {` branch) with:

```c
			} else if (r == USAGE_UNAUTHORIZED) {
				/* Token died mid-run. A REVOKED one never comes
				 * back from here -- worker_refresh_token reboots
				 * into provisioning -- so this branch only ever
				 * handles a transport failure. It used to discard
				 * the return and never classify, which left a
				 * revoked key hot-looping two full handshakes
				 * every 5 s, silently, forever. */
				if (worker_refresh_token(tok.refresh, &tok,
							 &token_deadline) == 0) {
					next_poll = now + 5 * 1000;
				} else {
					post_status(USAGE_STATUS_ERROR);
					next_poll = now + 60 * 1000;
				}
```

- [ ] **Step 5: Build**

Run the build command from Global Constraints.
Expected: links clean, no warnings about unused `REFRESH_RETRY_*`.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/main.c
git commit -m "OAuth: one worker refresh path that backs off and re-provisions

The proactive refresh never advanced its deadline on failure, so it re-fired a
full TLS handshake every 250 ms; the mid-run 401 never classified its failure,
so a revoked token hot-looped every 5 s with nothing on screen. Both now share
the boot path's helper: transport backs off, rejection re-provisions."
```

---

### Task 4: Move the boot refresh inside the loop that owns rejoin

The retry loop added on 2026-07-27 sits *above* the `while (1)` that holds the only WiFi rejoin path. If the AP drops during the first refresh — the single likeliest moment, right after a join — the loop sleeps 15/30/60/120/240/300 s forever and never reaches `main.c:704`, whose own comment says that without it "the board therefore stayed offline until it was power-cycled". `net_time_sync(10)` is likewise stranded above the loop, so the clock stays unset even after the AP returns, and `post_stage(2)` never posts, freezing the boot bar on "Sign in to Anthropic". The reboot this loop replaced was the only escape.

It also posts `USAGE_STATUS_ERROR` while `have_data` is false. At that point `usage_view`'s overlay *is* the boot screen — it owns the CONNECTING label and the segmented bar — and `usage_view.c:671` restores it only for `DISCONNECTED && !have_data`. Every other status hides it. So each retry tore down the boot takeover and left bare `--%` gauges reading "error - showing last known" when there is no last known, and nothing ever put it back. `DISCONNECTED` is both the correct state and what the pre-existing rejoin path posts for the same physical condition.

Making the refresh a state inside the loop fixes all of it: rejoin runs first every pass, the clock re-syncs, the backoff shares one ladder.

**Files:**
- Modify: `firmware/src/main.c:220-224` (the `REFRESH_RETRY_*` comment), `firmware/src/main.c:635-691` (`net_worker` prologue and loop head)

**Interfaces:**
- Consumes: `worker_refresh_token()` from Task 3.

- [ ] **Step 1: Correct the REFRESH_RETRY comment and shorten the first wait**

Replace `firmware/src/main.c:220-224` with:

```c
/* Same shape for a refresh that failed on transport rather than credentials.
 * Used by the not-yet-signed-in state at the top of the worker loop and by the
 * proactive pre-expiry refresh below it.
 *
 * 10 s first, not 15: on an OTA test boot the image has 90 s to prove itself,
 * and 10/20/40 fits four attempts inside that window where 15/30/60 fits three.
 * The cap is unreachable on a test boot by design -- an image that cannot reach
 * Anthropic in 90 s has not proven itself and SHOULD revert. */
#define REFRESH_RETRY_MIN_MS (10 * 1000)
#define REFRESH_RETRY_MAX_MS (5 * 60 * 1000)
```

- [ ] **Step 2: Replace the prologue and loop head**

Replace `firmware/src/main.c:635-691` — from `static void net_worker(void *a, void *b, void *c)` through the `while (1) {` and its `int64_t now = k_uptime_get();` — with:

```c
static void net_worker(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

	struct oauth_tokens tok;
	int64_t token_deadline = 0;
	int64_t next_poll = 0;
	int64_t next_tz = 0;	/* fetch as soon as we are online */
	int64_t next_ota = k_uptime_get() + 5 * 60 * 1000; /* first check 5 min in */
	int32_t tz_min = 0;
	int64_t next_rejoin = 0;
	int rejoin_wait_ms = REJOIN_WAIT_MIN_MS;
	int64_t next_refresh = 0;
	int refresh_wait_ms = REFRESH_RETRY_MIN_MS;
	bool authed = false;

	/* Advance the boot bar before each blocking step: visible progress on
	 * one screen instead of a title per stage (user request 2026-07-15).
	 * The clock sync rides inside the sign-in stage -- TLS needs the clock,
	 * and it is too quick to deserve a segment. */
	post_stage(1);

	while (1) {
		int64_t now = k_uptime_get();
```

- [ ] **Step 3: Add the sign-in state directly after the rejoin block**

Insert into `firmware/src/main.c` between the rejoin block's closing `continue;	/* nothing else works while offline */\n\t\t}` and the proactive-refresh block:

```c
		/*
		 * Not signed in yet. This used to run ABOVE the loop, which put
		 * it above the only rejoin path there is: a link drop during
		 * the first refresh -- the likeliest moment, right after a join
		 * -- wedged this thread in a backoff sleep forever, with no
		 * rejoin, no clock sync, and the boot bar frozen on "Sign in to
		 * Anthropic" until someone power-cycled the board. As a loop
		 * state it gets the rejoin above it for free.
		 */
		if (!authed) {
			if (now < next_refresh) {
				k_sleep(K_MSEC(250));
				continue;
			}

			/* TLS certificate checks need a real wall clock. Inside
			 * the loop so it re-runs after a rejoin, not once before
			 * the network was ever up. */
			if (!net_time_valid()) {
				net_time_sync(10);
			}

			int rc = worker_refresh_token(worker_refresh, &tok,
						      &token_deadline);

			if (rc == 0) {
				authed = true;
				refresh_wait_ms = REFRESH_RETRY_MIN_MS;
				next_poll = 0;
				post_stage(2);
				continue;
			}

			/* DISCONNECTED, not ERROR: with no data yet, usage_view's
			 * overlay IS the boot screen, and it restores that screen
			 * only for DISCONNECTED. Posting ERROR here hid the
			 * CONNECTING bar behind bare "--%" gauges captioned
			 * "error - showing last known" -- when there was no last
			 * known -- and nothing ever brought it back. */
			post_status(USAGE_STATUS_DISCONNECTED);
			printk("[oauth] refresh failed (%d) -- transport, token kept, retry in %d s\n",
			       rc, refresh_wait_ms / 1000);
			next_refresh = now + refresh_wait_ms;
			refresh_wait_ms = MIN(refresh_wait_ms * 2,
					      REFRESH_RETRY_MAX_MS);
			continue;
		}
```

- [ ] **Step 4: Verify nothing above the loop survives**

```bash
cd ~/Projects/LiveClaudeUi
sed -n '/^static void net_worker/,/^	while (1) {/p' firmware/src/main.c
```

Expected: only declarations and the single `post_stage(1)` between the signature and `while (1) {`. No `oauth_refresh`, no `net_time_sync`, no `cfg_set_token`.

- [ ] **Step 5: Build**

Run the build command from Global Constraints.
Expected: links clean. No `-Wunused-variable` for `token_deadline` or `authed`.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/main.c
git commit -m "Net: move the boot refresh inside the loop that owns rejoin

Above the loop it was above the only rejoin path: a link drop during the first
refresh wedged the worker in a backoff sleep with no rejoin and no clock sync
until a power cycle. It also posted ERROR while the boot screen was still up,
tearing down the CONNECTING bar for gauges that had nothing to show."
```

---

### Task 5: Finish the transition before deleting the screen, and absorb the input it held back

`pump_ms(UI_SLIDE_SETTLE_MS)` samples LVGL on a fixed clock, and at the ~124 ms full redraw this panel costs, each `pump_ms` iteration takes ~129 ms — so the samples land at 0/129/258/387 ms. Against a 250 ms slide the third sample completed it. Against the 400 ms slide added on 2026-07-26 the fourth lands at 387 and does not, so `lv_obj_del(scr)` runs mid-transition. LVGL clears `disp->prev_scr` only in `scr_anim_completed`; `lv_obj_delete` has no `prev_scr` handling at all. The next `lv_timer_handler()` from the mode loop then lays out and draws a freed screen tree whose memory the same pass has already recycled from the 30 KB pool.

The same unfinished transition is why the `pending = false` added in that commit does not stop the clip replaying. LVGL refuses to read any input while `prev_scr` is non-NULL, so the touch samples buffered during the slide are not dropped — they sit in the 96-deep msgq and flush on the mode loop's next handler pass, which runs *before* `ui_anim_pending()` is checked. The tail of the exit swipe then re-requests the clip after we cleared the flag.

Both go away if the settle waits for the actual signal instead of a stopwatch. `lv_display_get_screen_prev()` is public and returns exactly that state. Passing `auto_del = true` on the exit hands the delete to LVGL's own completion callback, so there is no window at all.

**Files:**
- Modify: `firmware/src/ui_anim.h:11-29`, `firmware/src/ui_anim.c:64-74` (`pump_ms`), `firmware/src/ui_anim.c:112-116` (entry), `firmware/src/ui_anim.c:155-185` (exit)

- [ ] **Step 1: Confirm the LVGL accessor exists in this tree**

```bash
grep -n "lv_display_get_screen_prev" ~/zephyrproject/modules/lib/gui/lvgl/src/display/lv_display.h
```

Expected: one declaration. It falls back to the default display when passed NULL — verified in `lv_display.c:639-648`.

- [ ] **Step 2: Replace pump_ms with a transition-aware settle**

Replace `firmware/src/ui_anim.c:64-74` (the `pump_ms` comment and function) with:

```c
/*
 * Wait for a screen-load transition to actually finish, then absorb the input
 * it held back.
 *
 * A fixed sleep was wrong twice over. LVGL animates the transition from the
 * timer handler, and each handler pass here costs ~129 ms at this panel's
 * ~124 ms full redraw -- so against a 400 ms slide the samples land at
 * 0/129/258/387 and the last one is still inside the transition. Returning
 * there meant deleting a screen LVGL still held in disp->prev_scr, which it
 * lays out and draws on the next pass.
 *
 * And LVGL blocks ALL input while prev_scr is set (lv_indev.c), so the touch
 * samples taken during the slide are not dropped -- they queue, and flush on
 * the mode loop's next handler pass, which runs before ui_anim_pending() is
 * read. That is how the tail of an exit swipe re-requested the clip and
 * replayed it. Pumping a few passes after the transition clears delivers them
 * while we are still here to drop the request.
 *
 * The deadline is a hang guard, not a schedule.
 */
static void settle_transition(void)
{
	int64_t deadline = k_uptime_get() + UI_SLIDE_MS * 4;

	while (lv_display_get_screen_prev(NULL) != NULL &&
	       k_uptime_get() < deadline) {
		lv_timer_handler();
		k_sleep(K_MSEC(5));
	}
	for (int i = 0; i < 4; i++) {
		lv_timer_handler();
		k_sleep(K_MSEC(5));
	}
}
```

- [ ] **Step 3: Use it on the way in**

Replace `firmware/src/ui_anim.c:115-116` with:

```c
	lv_scr_load_anim(scr, LV_SCR_LOAD_ANIM_MOVE_RIGHT, UI_SLIDE_MS, 0, false);
	settle_transition();
```

(`false` is load-bearing: `auto_del` here would delete the gauge screen we are coming back to.)

- [ ] **Step 4: Let LVGL delete the clip screen on the way out**

Replace `firmware/src/ui_anim.c:158-185` — the exit comment, both calls, `lv_obj_del(scr)`, the 20-line replay block and `pending = false` — with:

```c
	/* Slide the gauges back. auto_del = true hands the clip screen to
	 * LVGL's own completion callback, which is the only place that both
	 * deletes it and clears disp->prev_scr; deleting it ourselves left that
	 * pointer live whenever the transition had not finished. */
	lv_scr_load_anim(prev, LV_SCR_LOAD_ANIM_MOVE_LEFT, UI_SLIDE_MS, 0, true);
	settle_transition();

	/* Drop any request the exit swipe itself raised. A right swipe on the
	 * gauge screen is exactly what ASKS for the clip, so the tail of the
	 * gesture -- or a release landing on the left edge zone -- sets pending
	 * again through ui_settings' handlers. settle_transition above has
	 * already let those events land, so clearing here is the last word. */
	pending = false;
}
```

- [ ] **Step 5: Drop the now-unused settle constant**

Replace `firmware/src/ui_anim.h:11-29` (the comment block and both defines) with:

```c
/*
 * How long a screen transition runs.
 *
 * 400 ms, up from 250. Measured on hardware 2026-07-26: a full-screen redraw
 * costs ~124 ms, so a 250 ms transition got through barely two frames and read
 * as a jump cut rather than movement. This buys no extra frames per second --
 * nothing about rendering got faster -- it spreads the few frames the panel can
 * afford across enough time that the eye reads them as travel instead of a
 * stutter.
 *
 * There is no companion SETTLE constant any more. ui_anim waits on LVGL's own
 * prev_scr signal instead of a timeout, because a timeout tuned to one slide
 * duration silently stopped covering the next one.
 */
#define UI_SLIDE_MS 400
```

Verify nothing else referenced it:

```bash
grep -rn "UI_SLIDE_SETTLE_MS\|pump_ms" ~/Projects/LiveClaudeUi/firmware/src/
```

Expected: no matches.

- [ ] **Step 6: Build**

Run the build command from Global Constraints.
Expected: links clean.

- [ ] **Step 7: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/ui_anim.c firmware/src/ui_anim.h
git commit -m "Anim: wait for the transition instead of guessing at it

At ~129 ms per pump pass the fixed settle returned inside a 400 ms slide, so
lv_obj_del ran while LVGL still held the screen in prev_scr. Waiting on
prev_scr and letting auto_del do the delete closes that, and the extra passes
absorb the touch input the slide held back -- which is what replayed the clip."
```

---

### Task 6: Feed the watchdog while the clip plays

`ota_boot_pump()` is the only thing that feeds the hardware watchdog and the only thing that calls `boot_write_img_confirmed()`. It is called from the two mode loops and from `pump_ui` — none of which run inside `ui_anim_run`, whose player loop is unbounded (`while (!leave)`). On a post-OTA test boot the watchdog is armed at `window.max = 30000, WDT_FLAG_RESET_SOC`, so watching the clip for 30 seconds hard-resets the chip with the image still unconfirmed, and MCUboot reverts it.

Worse in standalone: `standalone_anim_pump` drains `net_evtq`, so a `NEV_USAGE` arriving mid-clip *does* set `ota_health = true` — the board proves itself healthy and then reverts anyway, because the only code that acts on that flag is unreachable.

The two anim pumps are the exact seam. `idle_until` calls them every ~5 ms.

**Files:**
- Modify: `firmware/src/main.c:595-610` (`standalone_anim_pump` and `usb_anim_pump`)

- [ ] **Step 1: Confirm the gap**

```bash
cd ~/Projects/LiveClaudeUi
grep -n "wdt_feed\|boot_write_img_confirmed\|ota_boot_pump()" firmware/src/main.c
```

Expected: `wdt_feed` and `boot_write_img_confirmed` appear only inside `ota_boot_pump`, and every `ota_boot_pump()` call is in `pump_ui` or one of the two mode loops — none in the anim pumps.

- [ ] **Step 2: Pump the OTA boot state from both anim pumps**

Replace `firmware/src/main.c:595-610` with:

```c
/* Between boot-clip frames (ui_anim_run) the mode's background duties keep
 * running through these: standalone drains the worker's queue, USB keeps the
 * serial protocol alive, and both feed the OTA test-boot state.
 *
 * That last one is not optional. ota_boot_pump is the only watchdog feeder and
 * the only caller of boot_write_img_confirmed, and the clip player's loop is
 * unbounded -- so on a test boot, watching the eyes for 30 s used to trip the
 * watchdog and revert a healthy image. Standalone made it worse: draining the
 * queue below sets ota_health, so the board proved itself and reverted anyway.
 */
static void standalone_anim_pump(void)
{
	struct net_evt e;

	while (k_msgq_get(&net_evtq, &e, K_NO_WAIT) == 0) {
		apply_net_evt(&e);
	}
	ota_boot_pump();
}

static void usb_anim_pump(void)
{
	proto_service();
	if (usage_view_have_data()) {
		ota_health = true;	/* daemon delivered usage */
	}
	ota_boot_pump();
}
```

- [ ] **Step 3: Build**

Run the build command from Global Constraints.
Expected: links clean. `ota_boot_pump` is defined at `main.c:118`, well above both pumps, so no forward declaration is needed.

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/main.c
git commit -m "OTA: keep feeding the watchdog while the boot clip plays

ota_boot_pump is the only watchdog feeder and the only writer of
boot_write_img_confirmed, and nothing called it inside the clip player's
unbounded loop -- so 30 s of watching the eyes on a test boot reset the chip
and reverted a good image."
```

---

### Task 7: Let a swipe that starts on the back chevron close settings

`lv_obj_set_ext_click_area(back, 12)` extends *gesture* hit-testing, not just clicks: `lv_obj_get_click_area` applies `ext_click_pad`, `lv_indev_search_obj` uses it, and the object it returns becomes `indev->pointer.act_obj`, which is the gesture origin. `back` has `GESTURE_BUBBLE` cleared, so the parent walk that would reach `panel_gesture_cb` never happens, and `back` has no gesture handler of its own — the gesture dies silently. Counting z-order, 814 px that used to close settings now do nothing, including all 3×30 of the green seam whose comment at line 653 promises "Tap or swipe-right both go home".

Setting the flag lets the gesture bubble to `panel`, whose handler closes on a right swipe. `panel_gesture_cb` reads the direction from the indev and ignores the event target, so bubbling costs it nothing. If LVGL also delivers `CLICKED` on the same release, `back_cb` calls `close_panel()` a second time and its `closing` guard makes that a no-op.

**Files:**
- Modify: `firmware/src/ui_settings.c:664-689`

- [ ] **Step 1: Restore gesture bubbling on the chevron**

In `firmware/src/ui_settings.c`, replace line 674:

```c
	lv_obj_clear_flag(back, LV_OBJ_FLAG_GESTURE_BUBBLE);
```

with:

```c
	/* Gestures MUST bubble from here. The extended touch area below covers
	 * the green seam and a strip of bare panel, and whatever the hit test
	 * lands on becomes the gesture's origin -- so with bubbling off, a
	 * swipe-right starting anywhere in that region died on this button
	 * instead of reaching panel_gesture_cb, silently un-closing ~814 px of
	 * a panel whose own seam comment promises the swipe works.
	 * close_panel() is idempotent (its `closing` guard), so a release that
	 * also fires back_cb costs nothing. */
	lv_obj_add_flag(back, LV_OBJ_FLAG_GESTURE_BUBBLE);
```

- [ ] **Step 2: Correct the extended-area dimensions in the comment**

In the block at lines 675-687, replace the sentence `but answers to a 64x50 region.` with:

```
 * but answers to a region 12 px larger on every side. That is 64x50 nominally;
 * 10 px of it falls off the top and left edges of the screen, so the reachable
 * area is 54x40.
```

- [ ] **Step 3: Build**

Run the build command from Global Constraints.
Expected: links clean.

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/src/ui_settings.c
git commit -m "Settings: stop the chevron's touch area swallowing swipe-to-close

ext_click_area extends gesture hit-testing too, and with GESTURE_BUBBLE cleared
the swipe died on a button with no gesture handler -- taking the green seam,
which exists to be swiped, with it."
```

---

### Task 8: Correct two prj.conf comments that state wrong numbers

Neither of these changes behaviour; both comments are load-bearing documentation that is arithmetically wrong, and one of them is the basis for a "we have no DRAM headroom" argument.

The VDB comment computes 4% of 320×240 as 6144 bytes. The Zephyr glue sizes the buffer with `LV_Z_BITS_PER_PIXEL`, and that Kconfig lists an unconditional `default 32` *before* `default 16 if LV_COLOR_DEPTH_16` — first default wins, so it is 32 despite this build's 16-bit colour. The real buffer is 12288 bytes, confirmed both in the generated `.config` and in the map (`.bss.buf0_0 = 0x3000`). Note the review's framing that this is "6 KB of padding" is wrong: the whole byte count is passed to `lv_display_set_buffers`, so LVGL uses all of it — 6144 RGB565 pixels, 8% of the screen, ~13 passes per full redraw rather than 25.

The refresh-period comment claims 16 ms and credits it with smoother countdown digits. Every `lv_timer_handler()` call site in this firmware sleeps 10 ms between passes, so a 16 ms gate lands on the second eligible pass — an effective ~20 ms. And the countdown text is driven by `usage_view_tick_1s()` behind a `now - last_tick >= 1000` guard, so it changes at 1 Hz regardless of this setting.

**Files:**
- Modify: `firmware/prj.conf:77-88`, `firmware/prj.conf:89-93`

- [ ] **Step 1: Re-confirm both numbers from the build output**

```bash
cd ~/Projects/LiveClaudeUi
grep -n "LV_Z_BITS_PER_PIXEL\|LV_Z_VDB_SIZE\|LV_COLOR_DEPTH=" firmware/build-sb/firmware/zephyr/.config
grep -n "bss.buf0_0" firmware/build-sb/firmware/zephyr/zephyr_final.map
```

Expected: `CONFIG_LV_Z_BITS_PER_PIXEL=32`, `CONFIG_LV_Z_VDB_SIZE=4`, `CONFIG_LV_COLOR_DEPTH=16`, and `.bss.buf0_0 ... 0x3000`.

- [ ] **Step 2: Replace the VDB comment**

Replace `firmware/prj.conf:77-88` (from `# Partial rendering:` through `CONFIG_LV_Z_VDB_SIZE=4`) with:

```
# Partial rendering, NOT a 150 KB framebuffer: there is no PSRAM, and WiFi +
# mbedTLS still have to fit in the same 520 KB.
#
# The buffer is 12288 B, not the 6144 this comment used to claim. LV_Z_VDB_SIZE
# is a percentage of the screen, but the Zephyr glue turns it into bytes with
# LV_Z_BITS_PER_PIXEL (modules/lvgl/lvgl.c, BUFFER_SIZE), and that Kconfig lists
# an unconditional `default 32` BEFORE `default 16 if LV_COLOR_DEPTH_16` --
# first default wins, so it is 32 here despite our 16-bit colour.
# 32 * (4 * 320 * 240 / 100) / 8 = 12288, confirmed as .bss.buf0_0 = 0x3000.
#
# None of it is wasted: the full byte count goes to lv_display_set_buffers, so
# LVGL renders 6144 RGB565 pixels per pass -- 8% of the screen, and ~13
# render-and-flush passes for a full redraw, not 25.
#
# Setting CONFIG_LV_Z_BITS_PER_PIXEL=16 would make the number mean what it says
# and hand ~6 KB back, which is worth remembering: measured 2026-07-26, free
# DRAM is ~3.3 KB in dram0 (__bss_end 0x3ffdf300 vs heap sentry 0x3ffe0000)
# plus ~0.4 KB in dram1. It would also halve the flush chunk and double the
# pass count, so it is a lever for when DRAM gets tight, not a fix.
CONFIG_LV_Z_VDB_SIZE=4
```

- [ ] **Step 3: Replace the refresh-period comment**

Replace `firmware/prj.conf:89-93` (from `# LVGL's own frame gate.` through `CONFIG_LV_DEF_REFR_PERIOD=16`) with:

```
# LVGL's own frame gate. The default 33 ms caps everything at 30 FPS, which is
# irrelevant to a full-screen redraw (124 ms, bounded elsewhere) but does bind
# on the cheap partial redraws, which measure ~5 ms.
#
# 16 does NOT buy a 16 ms period. Every lv_timer_handler() call site here sleeps
# 10 ms between passes (both mode loops in main.c, ui_anim, ui_boot), so LVGL is
# asked to refresh at most every ~10 ms and a 16 ms gate lands on the second
# eligible pass -- an effective ~20 ms. Real, but half what the number suggests.
#
# It does nothing for the countdown digits: usage_view_tick_1s() is called
# behind a `now - last_tick >= 1000` guard, so that text changes at 1 Hz no
# matter what this is set to.
CONFIG_LV_DEF_REFR_PERIOD=16
```

- [ ] **Step 4: Build**

Run the build command from Global Constraints.
Expected: links clean, and the binary is byte-identical in behaviour — only comments changed.

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/LiveClaudeUi
git add firmware/prj.conf
git commit -m "Comments: correct the VDB and refresh-period arithmetic

LV_Z_BITS_PER_PIXEL defaults to 32 ahead of the COLOR_DEPTH_16 default, so the
buffer is 12288 B and a full redraw is ~13 passes, not 6144 B and 25. And a
16 ms refresh gate lands on the second 10 ms pass, so the period is ~20 ms --
and never touched the 1 Hz countdown it was credited with."
```

---

### Task 9: Flash and verify on hardware

Nothing above is done until it is on the board. Four of these changes are behavioural in ways only hardware shows: the SPI revert (touch and display), the transition rework (visual), the watchdog fix (test boot), and the swipe fix (touch). Two more — the refresh retry ladder and the transport-retry branch — have **never once run on hardware**; that is recorded as a launch blocker in memory, and the router test below is what clears it.

**Files:**
- No source changes. If a check fails, stop and fix the owning task rather than patching around it here.

- [ ] **Step 1: Flash the board**

```bash
cd ~/Projects/LiveClaudeUi
tools/flash_encrypted.sh
```

Expected: writes MCUboot at 0x1000 and the signed app at 0x20000, then the board reboots. If it reports a missing key at `~/.clauge/flash_key.bin`, stop — without it the board cannot be flashed again, ever.

- [ ] **Step 2: Watch it boot**

```bash
cd ~/Projects/LiveClaudeUi
source ~/zephyr-v4.4.0/.venv/bin/activate
python3 tools/passive_log.py $(ls /dev/cu.usbserial* | head -1)
```

Expected: `[usage] firmware boot OK`, the boot bar advancing through its three steps, and gauges with real percentages within ~30 s. No `[oauth] refresh returned an empty token`. Leave this running for the checks below.

- [ ] **Step 3: Check touch, which Task 1 should have restored**

Tap the settings chevron on the right edge ten times. Expected: ten opens, no misses. Before Task 1 the XPT2046 was reading its buffer before the SPI words landed, so this is the check that says whether Task 7's extended touch area was ever the real problem.

- [ ] **Step 4: Check the swipe Task 7 restored**

Open settings, then swipe right starting **on the back chevron itself**, and again starting **on the green seam** at the top-left corner. Expected: the panel closes both times.

- [ ] **Step 5: Check the clip transition and the replay bug**

Swipe right on the gauges to start the clip. Expected: a smooth slide, no tearing, eyes on loop. Swipe back. Expected: gauges return and **stay** — the clip must not restart. Repeat ten times; the replay was intermittent, so once is not evidence.

- [ ] **Step 6: Check the watchdog fix**

With the clip playing, leave it running for a full 60 seconds without touching the screen. Expected: no reboot, no watchdog reset in the log. (On a non-test boot the watchdog is not armed, so this only proves the loop is healthy; Step 8 is the real test.)

- [ ] **Step 7: The router test — this is the one that clears two launch blockers**

Power the router off for 30 seconds, then back on, while the log runs.

Expected in order:
- `[wifi] link down -- rejoining`, and the CONNECTING bar returns — **not** a red "error - showing last known" caption over `--%` gauges.
- `[wifi] rejoin failed, retry in 30 s` if the outage outlasts one attempt.
- `[wifi] rejoined` once the router is back, then gauges repopulate within ~60 s.

Then repeat with the router off *before* the board boots, so the first refresh fails on transport. Expected: `[oauth] refresh failed (...) -- transport, token kept, retry in 10 s`, the CONNECTING bar staying up throughout, and — critically — the board recovering on its own once the router returns, with no power cycle and no trip back to provisioning. **The stored token must survive.** If the board lands on the setup screen, Task 2 or Task 4 is wrong.

- [ ] **Step 8: Check an OTA test boot end to end**

Cut a release and install it from Settings → Software update, then start the clip during the 90 s confirm window and watch it for 40 s.

Expected: `[ota] image confirmed` still appears, no watchdog reset, no `Update failed, previous version restored.` popup on the next boot.

- [ ] **Step 9: Update the memory notes**

Both `wifi-rejoin-backoff-untested.md` and `oauth-transport-retry-untested.md` are marked ⚠ REMIND BEFORE LAUNCH pending exactly the Step 7 test. If it passed, rewrite them to record the date and result. If it failed, leave them and record what broke.

- [ ] **Step 10: Commit any memory updates**

```bash
cd ~/Projects/LiveClaudeUi
git status --short
```

Expected: clean (the memory directory lives outside the repo). If the tree is dirty, something in Steps 1-8 left a stray file — investigate before finishing.

- [ ] **Step 11: Measure the net worker's stack high-water mark**

Nothing currently does. Add `CONFIG_THREAD_ANALYZER=y` and `CONFIG_THREAD_ANALYZER_AUTO=y` for one run, or printk `k_thread_stack_space_get(&net_thread, &unused)` right after the first successful refresh and again after a proactive refresh. Record the number. Expected: if unused space is under ~1 KB, make `fresh` static in `worker_refresh_token` before launch (safe — it is called only from the net worker thread). Note this branch adds ~688 B of peak depth on the TLS path, taking the deepest chain from 1888 B to 2576 B on an 8192 B stack.

- [ ] **Step 12: Exercise the proactive refresh**

Steps 1-10 never reach it — it fires only ~5 min before token expiry, roughly 55 minutes in. Either leave the board running past a full token lifetime or temporarily shorten the deadline to force it. Expected: the backoff printk on failure and the stored token surviving.

- [ ] **Step 13: Watch for real gaps in Step 7's router test backoff ladder**

On Step 7's router test, watch for real gaps between `retry in 10 s`, `20 s` and `40 s`. Expected: the gaps are real and increasing. Before this branch's fix the stamp was stale and three attempts ran back-to-back, so this test would have passed without exercising the ladder it exists to prove.

- [ ] **Step 14: Run Step 3 (touch) before Step 4 (swipe) and treat the result as evidence**

Expected: if touch is reliable after the SPI revert, the "not really clickable" report of 2026-07-27 was an SPI bug rather than a hit-area bug, and the chevron's 12 px extension should be revisited rather than kept by default.

- [ ] **Step 15: Enter the clip ten times, watching for an entry that exits immediately**

The entry settle now deliberately drains buffered touch onto the newly-active clip screen, whose gesture handler sets `leave`. Expected: LVGL's `gesture_sent` latch prevents a second gesture from the same press, on every one of the ten entries — this is the one path hardware can falsify and reading cannot.

- [ ] **Step 16: Test an OTA revert, not just the confirm**

Boot a test image with the router off so `ota_health` never sets, start the clip, and watch it through the confirm window. Expected: the board reverts at ~90 s with `[ota] not healthy within 90 s` rather than resetting silently at 30 s. That watchdog-at-30 s to deadline-at-90 s transition is exactly what the watchdog task changed, and nothing else tests it.

---

## Notes on what this plan does not do

- **`CONFIG_LV_DEF_REFR_PERIOD` keeps its value.** The review is right that continuously-animating widgets now invalidate and flush about twice as often on the single thread that also drains `net_evtq` — a queue whose producers all use `K_NO_WAIT`, so overflow drops silently. But changing a rendering tuning knob without a measurement on hardware trades one guess for another. Task 8 documents the true effective period; if the gauges stutter or events go missing in Task 9, raising this to 33 is the first thing to try.
- **`CONFIG_LV_Z_BITS_PER_PIXEL` keeps its (default) value of 32.** The comment claimed the buffer was half its real size, which Task 8 fixes, but the bytes are not wasted — LVGL uses all of them for bigger flush chunks. Setting it to 16 is a ~6 KB DRAM lever for later, not a bug fix.
- **The remaining low-severity items are deliberately out of scope**, and should be raised separately if they matter: `oauth_creds_rejected` does not treat a 403, or a 200 carrying `invalid_grant`, as a rejection (both land on -EIO/-EINVAL and now retry forever instead of re-provisioning); and `oauth.c:203` reads `errno` after `zsock_close()`, which can clobber it.
