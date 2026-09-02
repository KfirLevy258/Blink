# Pip Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single execution-state dot with one mark per live session, put the gauges and header back the way they were, and stop the hint line carrying a session count.

**Architecture:** The decision of *what to draw* is a pure function in `fmt.c` — host-testable without LVGL, which is the only automated coverage this feature can have. `usage_view.c` owns a fixed array of pip objects and only positions and colours them. The layout revert is pure constants. No wire change: the three counts already reach the board and are discarded.

**Tech Stack:** C99 + LVGL 9 (firmware), standalone `cc` host tests, pytest for the daemon side (unchanged here).

**Spec:** `docs/session-name-hint-design.md` — read **Addendum 2** in particular; Addendum 1 describes the header this plan reverts.

## Global Constraints

- **No wire change.** The `usage` message measures 510 of a 512-byte limit and `proto.c` drops an over-long line whole. `n_run`, `n_wait`, `n_stuck` and `n_sess` already arrive; finished is `n_sess − n_run − n_wait − n_stuck`.
- **Three colours only, no new meanings:** `COL_RED` failed, `COL_AMBER` needs you (waiting *or* finished), `COL_GREEN` working. Do not introduce a fourth hue and do not use `COL_GREY` for a session state — grey means *host gone* on this panel.
- **Fixed order, most urgent first:** failed, waiting, running, finished.
- **Pip geometry:** 8 px pip, 11 px pitch, row starts at **x=56**, hard wall at **x=130**. Budget **75 px**.
- **Mode threshold:** 1–6 sessions draw one pip each; 7+ draw one pip per non-empty state with its count.
- **Counts mode fits three groups.** On overflow drop `finished` first, then `running`.
- UI copy is sentence case. No status string may contain `-` — it is the hint line's separator.
- Firmware buffer bounds come from the destination, never from trusting the daemon.
- **Firmware is not done until flashed and boot-verified.** `usage_view.c` cannot be host-compiled.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `firmware/src/usage_layout.h` | every coordinate | revert header + ring; add pip constants |
| `firmware/src/fmt.h` / `fmt.c` | pure formatting, host-tested | add `fmt_pips()`; drop `fmt_hint`'s count arm |
| `firmware/src/usage_view.c` | widgets and state | clock back to the corner; pip array replaces `act_dot` |
| `firmware/src/proto.c` | wire dispatch | parse the three counts it discards today |
| `tests/fmt/host_test.c` | pure-logic coverage | `fmt_pips` cases |
| `tests/usage_layout/host_test.c` | geometry assertions | reverted stack + the brand-wall guard |

Task 1 is constants only. Task 2 is pure logic. Task 3 is the wire. Task 4 draws. Task 5 is hardware. Tasks 1–3 are independent of each other; Task 4 needs all three.

---

### Task 1: Put the header and the rings back

**Files:**
- Modify: `firmware/src/usage_layout.h`, `firmware/src/usage_view.c`
- Test: `tests/usage_layout/host_test.c`

**Interfaces:**
- Consumes: nothing.
- Produces: `TITLE_Y 6`, `STATUS_Y 24`, `GAUGE_ARC_Y 44`, `GAUGE_ARC_SZ 120`, and new `PIP_SZ`, `PIP_PITCH`, `PIP_X0`, `PIP_WALL_X`, `PIP_MAX`. Task 4 positions against all five.

- [ ] **Step 1: Write the failing assertions**

Replace the header-stack assertions in `tests/usage_layout/host_test.c` (the ones added for the three-row header) with the two-row arrangement, and add the pip-row guards. Use the file's real `EXPECT_EQ` and `CHECK(cond, msg)` macros — do not invent new ones:

```c
	/*
	 * Two header rows again, not three. The clock is back in the corner
	 * it briefly vacated, so the arcs get their 20 px back.
	 */
	EXPECT_EQ(STATUS_Y, TITLE_Y + FONT_LINE_H + 2);
	EXPECT_EQ(GAUGE_ARC_Y, STATUS_Y + FONT_LINE_H + 4);
	EXPECT_EQ(GAUGE_ARC_Y + GAUGE_ARC_SZ + 4, GAUGE_NAME_Y);

	/* The percentage stays centre-derived; at 120 that lands where the
	 * literal 90 used to be, which is the check that proves the
	 * expression was the right shape all along. */
	EXPECT_EQ(GAUGE_PCT_Y, 90);

	/*
	 * The pip row lives between the clock and the brand. Both edges are
	 * asserted because both are measurements, not constants the code
	 * knows: the clock's width comes from "12:04" at montserrat_14, and
	 * the wall from "BLINK" centred with .09em tracking. A font bump
	 * must fail here rather than slide pips under the logo.
	 */
	CHECK(PIP_X0 > 47, "pip row starts clear of the clock");
	CHECK(PIP_X0 + PIP_MAX * PIP_PITCH - (PIP_PITCH - PIP_SZ) <= PIP_WALL_X,
	      "a full pip row clears the brand");
	CHECK(PIP_WALL_X < 136, "the wall is left of the brand's left edge");
	EXPECT_EQ(PIP_MAX, 7);
```

- [ ] **Step 2: Run to verify they fail**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `usage_layout BUILD FAILED` — `PIP_X0` undeclared.

- [ ] **Step 3: Revert the header constants**

In `firmware/src/usage_layout.h`, set `TITLE_Y` back to `6`, `STATUS_Y` back to `24`, `GAUGE_ARC_Y` back to `44`, `GAUGE_ARC_SZ` back to `120`, and **delete `CLOCK_Y` entirely**. Leave `GAUGE_PCT_Y` as the expression it became — do not restore a literal.

Replace the `CLOCK_Y` comment block with a note explaining the reversal in the file's voice:

```c
/*
 * The clock is back in the top-left corner, and the rings are back to 120.
 *
 * It moved to a row of its own to free that corner for an execution-state
 * indicator, and the whole cost of the extra row came out of the ring --
 * 120 down to 100 -- because everything below the arcs is pinned. The
 * indicator is now a row of pips in the gap between the clock and the brand,
 * which was empty the entire time, so the row was never needed and the ring
 * gets its 20 px back.
 */
```

- [ ] **Step 4: Add the pip constants**

Below `DOT_SZ`:

```c
/*
 * The pip row: one mark per live session, in the gap between the clock and
 * the brand.
 *
 * Both bounds are MEASUREMENTS, which is why the layout test asserts them
 * rather than trusting them. "12:04" at montserrat_14 ends near x=47, and
 * "BLINK" is centred at 160 with .09em tracking and about 47 px wide, so it
 * begins near x=136. An 8 px gap either side leaves 75 px.
 *
 * PIP_MAX is geometry, not policy: seven fit. The display switches to counts
 * at SIX, which is where a row stops being read and starts being counted --
 * see fmt_pips(). The extra slot is headroom, not a target.
 */
#define PIP_SZ			8
#define PIP_PITCH		11
#define PIP_X0			56
#define PIP_WALL_X		130
#define PIP_MAX			7
```

- [ ] **Step 5: Put the clock back in the corner**

In `firmware/src/usage_view.c`, find the clock's alignment (currently `lv_obj_align(clock_lbl, LV_ALIGN_TOP_MID, 0, CLOCK_Y)`) and restore it:

```c
	lv_obj_align(clock_lbl, LV_ALIGN_TOP_LEFT, 10, HDR_ROW_Y);
```

Change nothing else about the label.

- [ ] **Step 6: Run to verify they pass**

Run: `sh tests/ci/check_host_tests.sh`
Expected: all 14 pass, `usage_layout` included.

- [ ] **Step 7: Build**

Run: `west build -b esp32_devkitc/esp32/procpu` from `firmware/`. Never flash.
Expected: clean, no new warnings.

- [ ] **Step 8: Commit**

```bash
git add firmware/src/usage_layout.h firmware/src/usage_view.c tests/usage_layout/host_test.c
git commit -m "feat: the clock takes its corner back and the rings return to 120"
```

---

### Task 2: `fmt_pips()` decides what to draw

Pure logic, no LVGL. This is the only part of the feature that can be tested automatically, so it carries the whole decision — mode, order, overflow — and `usage_view.c` is left with nothing to decide.

**Files:**
- Modify: `firmware/src/fmt.h`, `firmware/src/fmt.c`
- Test: `tests/fmt/host_test.c`

**Interfaces:**
- Consumes: nothing.
- Produces:

```c
enum fmt_pip_kind { FMT_PIP_FAILED, FMT_PIP_WAITING, FMT_PIP_RUNNING, FMT_PIP_FINISHED };

struct fmt_pip { enum fmt_pip_kind kind; int count; };

int fmt_pips(int n_run, int n_wait, int n_fail, int n_fin,
             struct fmt_pip *out, int max);
```

Returns how many entries were written. `count` is 0 in pip mode (one entry per session, the count is meaningless) and the state's tally in counts mode. Task 4 reads both.

- [ ] **Step 1: Write the failing test**

Append to `tests/fmt/host_test.c` and register in `main()`:

```c
static void test_fmt_pips(void)
{
	struct fmt_pip p[8];
	int n;

	/* Nothing running: an empty corner is true. */
	n = fmt_pips(0, 0, 0, 0, p, 8);
	EXPECT_EQ(n, 0);

	/* One session, one pip, count unused. */
	n = fmt_pips(1, 0, 0, 0, p, 8);
	EXPECT_EQ(n, 1);
	EXPECT_EQ((int)p[0].kind, (int)FMT_PIP_RUNNING);
	EXPECT_EQ(p[0].count, 0);

	/* Fixed order, most urgent first, regardless of argument order. */
	n = fmt_pips(1, 1, 1, 1, p, 8);
	EXPECT_EQ(n, 4);
	EXPECT_EQ((int)p[0].kind, (int)FMT_PIP_FAILED);
	EXPECT_EQ((int)p[1].kind, (int)FMT_PIP_WAITING);
	EXPECT_EQ((int)p[2].kind, (int)FMT_PIP_RUNNING);
	EXPECT_EQ((int)p[3].kind, (int)FMT_PIP_FINISHED);

	/* Six is the last pip-mode count: six entries, every count still 0. */
	n = fmt_pips(4, 1, 0, 1, p, 8);
	EXPECT_EQ(n, 6);
	EXPECT_EQ(p[5].count, 0);

	/* Seven flips to counts mode: one entry per NON-EMPTY state, each
	 * carrying its tally. 4 running + 2 waiting + 1 finished = 3 groups. */
	n = fmt_pips(4, 2, 0, 1, p, 8);
	EXPECT_EQ(n, 3);
	EXPECT_EQ((int)p[0].kind, (int)FMT_PIP_WAITING);
	EXPECT_EQ(p[0].count, 2);
	EXPECT_EQ((int)p[1].kind, (int)FMT_PIP_RUNNING);
	EXPECT_EQ(p[1].count, 4);
	EXPECT_EQ((int)p[2].kind, (int)FMT_PIP_FINISHED);
	EXPECT_EQ(p[2].count, 1);

	/* Four groups do not fit 75 px, so finished is dropped first. */
	n = fmt_pips(4, 2, 1, 1, p, 8);
	EXPECT_EQ(n, 3);
	EXPECT_EQ((int)p[0].kind, (int)FMT_PIP_FAILED);
	EXPECT_EQ((int)p[1].kind, (int)FMT_PIP_WAITING);
	EXPECT_EQ((int)p[2].kind, (int)FMT_PIP_RUNNING);

	/* A tiny `max` truncates rather than overruns. */
	n = fmt_pips(1, 1, 1, 1, p, 2);
	EXPECT_EQ(n, 2);
	EXPECT_EQ((int)p[0].kind, (int)FMT_PIP_FAILED);

	/* Negative counts are treated as zero, not as a state. */
	n = fmt_pips(-3, 0, 0, 0, p, 8);
	EXPECT_EQ(n, 0);

	/* Counts mode never grows: twenty sessions still fit three groups. */
	n = fmt_pips(12, 5, 2, 1, p, 8);
	EXPECT_EQ(n, 3);
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `fmt BUILD FAILED` — `fmt_pips` and `struct fmt_pip` undeclared.

- [ ] **Step 3: Declare it in `fmt.h`**

```c
/*
 * What the pip row should draw, decided here so usage_view.c only positions
 * and colours. Pure, so it is the one part of this feature a host test can
 * reach at all -- usage_view.c needs LVGL and cannot be compiled on a laptop.
 */
enum fmt_pip_kind {
	FMT_PIP_FAILED,		/* a turn died -- an event, never inferred */
	FMT_PIP_WAITING,	/* a prompt is open */
	FMT_PIP_RUNNING,	/* working */
	FMT_PIP_FINISHED,	/* done, unread -- amber like WAITING, see below */
};

struct fmt_pip {
	enum fmt_pip_kind kind;
	int count;		/* 0 in pip mode; the state's tally in counts mode */
};

/*
 * Fill `out` with what to draw, most urgent first, and return how many.
 *
 *   0 sessions        -> 0 entries. An empty corner is true.
 *   1-6 sessions      -> one entry per SESSION, count 0.
 *   7+                -> one entry per NON-EMPTY state, carrying its tally.
 *
 * Six is not the geometric limit -- seven pips fit in the 75 px between the
 * clock and the brand. It is where a row stops being read and starts being
 * counted, which is the opposite of what a desk display is for.
 *
 * Counts mode holds THREE groups in that space, not four, so an overflow
 * drops FINISHED first and then RUNNING -- the worst case still shows the two
 * states that actually need a person.
 */
int fmt_pips(int n_run, int n_wait, int n_fail, int n_fin,
	     struct fmt_pip *out, int max);
```

- [ ] **Step 4: Implement it in `fmt.c`**

```c
/* Three groups fit the 75 px between the clock and the brand; four do not. */
#define PIP_GROUPS_MAX	3
/* Above this many sessions a row of pips is counted rather than read. */
#define PIP_SESSIONS_MAX 6

int fmt_pips(int n_run, int n_wait, int n_fail, int n_fin,
	     struct fmt_pip *out, int max)
{
	if (!out || max <= 0) {
		return 0;
	}
	if (n_run < 0) { n_run = 0; }
	if (n_wait < 0) { n_wait = 0; }
	if (n_fail < 0) { n_fail = 0; }
	if (n_fin < 0) { n_fin = 0; }

	/* Most urgent first: the eye lands on the left of this row. */
	const enum fmt_pip_kind kinds[4] = {
		FMT_PIP_FAILED, FMT_PIP_WAITING, FMT_PIP_RUNNING, FMT_PIP_FINISHED
	};
	const int counts[4] = { n_fail, n_wait, n_run, n_fin };
	int total = n_fail + n_wait + n_run + n_fin;
	int w = 0;

	if (total == 0) {
		return 0;
	}

	if (total <= PIP_SESSIONS_MAX) {
		for (int k = 0; k < 4 && w < max; k++) {
			for (int j = 0; j < counts[k] && w < max; j++) {
				out[w].kind = kinds[k];
				out[w].count = 0;
				w++;
			}
		}
		return w;
	}

	/*
	 * Counts mode. Walking the array in urgency order and stopping at
	 * PIP_GROUPS_MAX drops FINISHED first and then RUNNING for free --
	 * they are simply last in line, so the rule needs no separate branch
	 * that could disagree with the ordering above.
	 */
	for (int k = 0; k < 4 && w < max && w < PIP_GROUPS_MAX; k++) {
		if (counts[k] == 0) {
			continue;
		}
		out[w].kind = kinds[k];
		out[w].count = counts[k];
		w++;
	}
	return w;
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `fmt` passes; all other host tests unchanged.

- [ ] **Step 6: Drop the count arm from `fmt_hint`**

The hint line no longer says `Working - 3 sessions` — the pips carry that. Remove the `n_sessions` parameter from `fmt_hint`'s declaration, its definition and the `n_sessions > 1` branch, so it composes only `status` or `status - label`. Update its header comment to say the count moved to the pip row, and cite `fmt_pips`.

Update the existing `fmt_hint` cases in `tests/fmt/host_test.c`: delete the two that assert the count form (`"Waiting for you - 3 sessions"` and the `"Finished"` singular case), and drop the argument from every remaining call.

- [ ] **Step 7: Run again**

Run: `sh tests/ci/check_host_tests.sh`
Expected: passes. The compiler catches any call site you missed — `usage_view.c` is not host-compiled, so grep it too: `grep -n fmt_hint firmware/src/usage_view.c` must show no three-argument calls left.

- [ ] **Step 8: Commit**

```bash
git add firmware/src/fmt.h firmware/src/fmt.c tests/fmt/host_test.c
git commit -m "feat: the pip row decides what to draw, and the hint line stops counting"
```

---

### Task 3: Parse the counts the board already receives

**Files:**
- Modify: `firmware/src/proto.c` (the `usage` branch, beside `num(json, "n_sess", …)`), `firmware/src/usage_view.h`
- Test: `tests/msg_parse/host_test.c`

**Interfaces:**
- Consumes: nothing.
- Produces: `void usage_view_set_counts(int n_sess, int n_run, int n_wait, int n_stuck);` — Task 4 implements it. `proto.c` calls it from the `usage` branch.

- [ ] **Step 1: Write the failing test**

Add to `tests/msg_parse/host_test.c`, matching the file's existing style and using `msg_get_double` (there is no `msg_get_num`):

```c
	/* The counts have always been on the wire; only n_sess was read. */
	{
		double v = -1;
		const char *j = "{\"t\":\"usage\",\"n_sess\":4,\"n_run\":2,"
				"\"n_wait\":1,\"n_stuck\":1}";

		EXPECT(msg_get_double(j, "n_run", &v));   EXPECT_EQ((int)v, 2);
		EXPECT(msg_get_double(j, "n_wait", &v));  EXPECT_EQ((int)v, 1);
		EXPECT(msg_get_double(j, "n_stuck", &v)); EXPECT_EQ((int)v, 1);
	}
	{
		/* Absent counts are the common case -- the daemon omits a zero. */
		double v = -1;
		const char *j = "{\"t\":\"usage\",\"n_sess\":1}";

		EXPECT(!msg_get_double(j, "n_run", &v));
	}
```

- [ ] **Step 2: Run to verify**

Run: `sh tests/ci/check_host_tests.sh`
Expected: passes immediately — `msg_get_double` already works. This test pins the wire shape so a future daemon change that renames a key fails here rather than silently emptying the row.

- [ ] **Step 3: Declare the setter**

In `firmware/src/usage_view.h`, beside `usage_view_set_activity`:

```c
/*
 * How many sessions are in each state, for the pip row.
 *
 * `n_stuck` is the wire's name and it carries FAILED -- claude_state folds
 * the two together and no provider produces `stuck` any more. Finished is not
 * sent: it is n_sess minus the other three, derived here rather than spending
 * bytes on a line that has two to spare.
 */
void usage_view_set_counts(int n_sess, int n_run, int n_wait, int n_stuck);
```

- [ ] **Step 4: Parse and forward**

In `proto.c`'s `usage` branch, beside the existing `n_sess` read:

```c
		double nr = 0, nw = 0, nst = 0;

		/* Sent since the counts existed, dropped on the floor until
		 * the pip row had a use for them. Absent means zero on both
		 * sides, which is why the daemon omits them. */
		num(json, "n_run", &nr, 0, 9999);
		num(json, "n_wait", &nw, 0, 9999);
		num(json, "n_stuck", &nst, 0, 9999);
		usage_view_set_counts((int)ns, (int)nr, (int)nw, (int)nst);
```

Place it after the existing `usage_view_set_sessions` call so the counts land with the rest of the frame. Use the same local `num()` helper the branch already uses.

- [ ] **Step 5: Build**

Run: `west build -b esp32_devkitc/esp32/procpu` from `firmware/`. Never flash.
Expected: fails to link with an undefined `usage_view_set_counts` — that is correct; Task 4 supplies it. Note the failure in your report and move on.

- [ ] **Step 6: Commit**

```bash
git add firmware/src/proto.c firmware/src/usage_view.h tests/msg_parse/host_test.c
git commit -m "feat: the board reads the session counts it has been discarding"
```

---

### Task 4: Draw the row

**Files:**
- Modify: `firmware/src/usage_view.c`
- Test: none automatable — `usage_view.c` needs LVGL. Task 5 is the gate.

**Interfaces:**
- Consumes: `PIP_SZ`, `PIP_PITCH`, `PIP_X0`, `PIP_MAX` (Task 1); `fmt_pips()` and `struct fmt_pip` (Task 2); the `usage_view_set_counts` declaration (Task 3).
- Produces: nothing later depends on.

- [ ] **Step 1: Replace `act_dot` with an array**

`act_dot` is a single object created around `usage_view.c:643`. Replace its declaration at line 130 and its creation block with an array of `PIP_MAX` objects plus their labels. **Read the existing `act_dot` creation and mirror every style call it makes** — including `LV_OBJ_FLAG_GESTURE_BUBBLE` — rather than trusting this snippet to be complete:

```c
static lv_obj_t *pip[PIP_MAX];		/* execution state, one per session */
static lv_obj_t *pip_num[PIP_MAX];	/* its tally, in counts mode only */
static int pip_n_sess, pip_n_run, pip_n_wait, pip_n_stuck;
```

Create all `PIP_MAX` pairs up front and hide them; showing and hiding is cheaper and steadier than creating and deleting objects on every poll. Position pip *i* at `PIP_X0 + i * PIP_PITCH`, and its numeral immediately after it.

- [ ] **Step 2: Store the counts**

```c
void usage_view_set_counts(int n_sess, int n_run, int n_wait, int n_stuck)
{
	pip_n_sess = n_sess;
	pip_n_run = n_run;
	pip_n_wait = n_wait;
	pip_n_stuck = n_stuck;
	refresh_dots();
}
```

- [ ] **Step 3: Draw the row inside `refresh_dots()`**

Replace the block that painted the single `act_dot` with one that asks `fmt_pips` and lays out the answer. Finished is derived here, clamped at zero so a frame whose parts disagree cannot produce a negative:

```c
	/*
	 * The execution axis is a row now, not a dot. fmt_pips owns every
	 * decision -- mode, order, overflow -- so this loop only positions
	 * and colours. See fmt.h for why six is the threshold.
	 */
	int fin = pip_n_sess - pip_n_run - pip_n_wait - pip_n_stuck;

	if (fin < 0) {
		fin = 0;
	}

	struct fmt_pip want[PIP_MAX];
	int n = fmt_pips(pip_n_run, pip_n_wait, pip_n_stuck, fin,
			 want, PIP_MAX);
	int x = PIP_X0;

	for (int i = 0; i < PIP_MAX; i++) {
		if (i >= n) {
			lv_obj_add_flag(pip[i], LV_OBJ_FLAG_HIDDEN);
			lv_obj_add_flag(pip_num[i], LV_OBJ_FLAG_HIDDEN);
			continue;
		}

		lv_color_t c = pip_colour(want[i].kind);

		lv_obj_clear_flag(pip[i], LV_OBJ_FLAG_HIDDEN);
		lv_obj_align(pip[i], LV_ALIGN_TOP_LEFT, x, HDR_ROW_Y + 2);
		lv_obj_set_style_bg_color(pip[i], c, 0);

		if (want[i].count > 0) {
			char b[8];

			snprintf(b, sizeof(b), "%d", want[i].count);
			lv_label_set_text(pip_num[i], b);
			lv_obj_set_style_text_color(pip_num[i], c, 0);
			lv_obj_align(pip_num[i], LV_ALIGN_TOP_LEFT,
				     x + PIP_SZ + 2, HDR_ROW_Y);
			lv_obj_clear_flag(pip_num[i], LV_OBJ_FLAG_HIDDEN);
			x += PIP_SZ + 2 + 9 + 7;
		} else {
			lv_obj_add_flag(pip_num[i], LV_OBJ_FLAG_HIDDEN);
			x += PIP_PITCH;
		}
	}
```

Add the colour helper beside `activity_color()`:

```c
/*
 * Three colours, and no new meanings: each pip reads exactly like the single
 * execution dot it replaces. WAITING and FINISHED share amber deliberately --
 * the panel already established it cannot separate them at this size, and an
 * 8 px pip is a finer channel than the pulse that already failed.
 */
static lv_color_t pip_colour(enum fmt_pip_kind k)
{
	switch (k) {
	case FMT_PIP_FAILED:	return COL_RED;
	case FMT_PIP_WAITING:	return COL_AMBER;
	case FMT_PIP_FINISHED:	return COL_AMBER;
	default:		return COL_GREEN;
	}
}
```

- [ ] **Step 4: Remove what the pips replaced**

The pip row is not a severity indicator, so nothing here pulses — delete the `act_pulse_cb` animation calls that targeted `act_dot`, and the `activity_color()` call that fed it. Leave `activity_color()` itself: `activity_text()` and the hint line still use its sibling logic, and `refresh_dots()` still paints the top-right health dot exactly as before. **Do not change the health dot.**

Grep for `act_dot` afterwards; it must be gone from the file entirely, comments included.

- [ ] **Step 5: Update the header comment in `usage_layout.h`**

Addendum 1's comment describes a single pip in the freed corner. It is now a row between the clock and the brand, and the corner is the clock's again. Correct it in the file's voice, keeping the `6540287` history and the reason this is not a revert of that decision.

- [ ] **Step 6: Build**

Run: `west build -b esp32_devkitc/esp32/procpu` from `firmware/`. Never flash.
Expected: clean, no warnings. This is the only mechanical check this task gets.

- [ ] **Step 7: Run every automated suite**

Run: `sh tests/ci/check_host_tests.sh`
Run: `/private/tmp/claude-502/-Users-KfirLevy-Projects-LiveClaudeUi/aeb3001d-3255-41ed-b053-ecf8e0cdec4c/scratchpad/venv/bin/python -m pytest tests -q`
Expected: all pass. Neither covers this file; they prove nothing else broke.

- [ ] **Step 8: Commit**

```bash
git add firmware/src/usage_view.c firmware/src/usage_layout.h
git commit -m "feat: one pip per session, between the clock and the brand"
```

---

### Task 5: Flash it and look at it

`usage_view.c` has no automated coverage, so this is the gate, not a formality. The board is on `/dev/cu.usbserial-14240` (it re-enumerates on reset — check `ls /dev/cu.usbserial*` first) and the installed daemon holds the port under launchd as `com.blink.bridge`.

**Files:** none.

- [ ] **Step 1: Free the port**

Run: `launchctl bootout gui/502/com.blink.bridge`, then confirm with `lsof /dev/cu.usbserial-*`.
Expected: port free.

- [ ] **Step 2: Flash**

Run: `PORT=<the port> bash tools/dev.sh flash`
Expected: the eFuse probe reports plaintext, the build succeeds, the write verifies its hash. If esptool reports `Device not configured`, wait three seconds and retry the write — the probe resets the chip and the port is briefly absent.

- [ ] **Step 3: Confirm it boots**

Run: `tail -f /tmp/claude-usage-bridge.log`
Expected: `welcome`, then `pref`/`ota_query`, then pings with `up_ms` climbing. A reset would show `up_ms` dropping back toward zero.

- [ ] **Step 4: Drive the pip counts**

With the dev daemon up, write probe state files into `~/.blink/state/` to produce known counts — `{"event":"Notification","t":<now>,"name":"X"}` is waiting, `PreToolUse` is running, `StopFailure` is failed, `Stop` is finished — then restart the daemon (`bash tools/dev.sh up`) to force an immediate push. Check 1, 3, 6, 7 and 12 sessions.
Expected at each: the row matches, and `up_ms` keeps climbing.

- [ ] **Step 5: Look at the panel — the part only a person can do**

- Do 6 pips read as *six*, or as a smear? That threshold is the whole design.
- At 7, does the row switch to counts and stay the same width at 12 and 20?
- Do the pips clear the clock on the left and the brand on the right?
- Are the rings visibly back to their old size, and is the percentage centred in them?
- Does the hint line still read `Waiting for you - <project>`, with no session count?

- [ ] **Step 6: Clean up and restore**

Run: `rm -f ~/.blink/state/zzprobe-*.state`, then `bash tools/dev.sh down`, then `launchctl bootstrap gui/502 ~/Library/LaunchAgents/com.blink.bridge.plist` **and** `launchctl kickstart gui/502/com.blink.bridge` — bootstrap registers without starting.
Expected: `blink status` shows the bridge running and the board found.

- [ ] **Step 7: Record what was seen**

Note the verified frames in `docs/next-steps.md` following the existing convention, and commit.

---

## Self-Review

**Spec coverage:** Addendum 2's three changes map to tasks — rings and header revert (Task 1), pip row with its threshold, order and overflow rule (Tasks 2 and 4), hint line losing its count (Task 2 Step 6). The colour rule is Task 4 Step 3. The `GAUGE_PCT_Y`-stays-an-expression ruling is Task 1 Step 3 with an assertion at Step 1. The wire constraint is honoured by Task 3 reading existing keys. Out-of-scope items (a sessions page, separating waiting from finished) have no tasks, by design.

**Type consistency:** `struct fmt_pip` and `enum fmt_pip_kind` are defined in Task 2 and consumed in Task 4. `fmt_pips(n_run, n_wait, n_fail, n_fin, out, max)` — Task 4 calls it with `(pip_n_run, pip_n_wait, pip_n_stuck, fin, …)`, matching the failed-is-n_stuck note in Task 3's header comment. `usage_view_set_counts` is declared in Task 3 and defined in Task 4. `PIP_*` constants are defined in Task 1 and used in Tasks 2 (as prose) and 4 (as code).

**Known soft spots, stated rather than hidden:** Task 4's numeral advance (`9` px) is an estimate of a montserrat_14 digit; the implementer should measure it against the font table the way Task 7 of the previous plan measured `lv_font_montserrat_20`, and say so in its report. Task 1's `EXPECT_EQ(STATUS_Y, TITLE_Y + FONT_LINE_H + 2)` encodes the old two-row spacing — if the reverted numbers do not satisfy it, the constant is wrong, not the assertion. Task 4 Step 1 deliberately refuses to list `act_dot`'s style calls, because the previous plan's equivalent list was incomplete and omitted a flag that mattered.
