# What Codex writes when the limit runs out

Measured on the owner's desk, 2026-09-05, by running the five-hour window out
for real. Until this happened nobody on this project had exhaustion data:
every Codex fixture in the suite had been written by us, from readings we
generated, none of which had ever been null.

## The transition

Two `token_count` events, half a second apart:

```
08:05:52.357Z   limit_id=codex     primary.used_percent=98    secondary=16
08:05:52.914Z   limit_id=premium   primary=null               secondary=null
```

Three things change at once:

1. **Both windows go null.** Not 100 — `null`. The `rate_limits` envelope
   itself survives, with `credits`, `plan_type` and the rest intact.
2. **`limit_id` flips** from `codex` to `premium`.
3. The climb stops at **98**, not 100. There is no final full reading; the
   last number Codex publishes always understates.

Everything after that point repeats the null envelope. `credits.has_credits`
was `false` and `balance` `"0"`, which is presumably why the bucket moved and
then had nothing to move to.

## What it broke

`parse_rollout_tail` stopped at the newest line containing `rate_limits` —
the null one. `parse_cli_event` correctly refuses a frame with no numbers, so
the caller dropped the whole file, including the 98 two lines above. The
freshest reading left anywhere was a twenty-minute-old 29 from a Codex
desktop thread, and the panel published that: another session's stale number,
shown as the current state of the account, at the one moment the dial had to
be right.

Fixed in `codex_cli.py` — scan past window-less envelopes to the newest one
carrying a number, report the session window full, and date the frame by the
exhaustion rather than by the older reading.

## What is NOT a usable signal

- **`usage_limit_reached`** appears in the file, but inside an
  `item_completed` payload's `item` — conversation content, not protocol —
  and in this session it appeared at 07:34, half an hour before the actual
  exhaustion at 08:05. Do not trigger on it.
- **The nulls alone.** Saturating on nulls would let a transient upstream
  omission tell someone they are blocked when they are not, which is worse
  than understating. The trigger is `limit_id` *changing*.

## An early refill is possible, and we cannot see it coming

The same session refilled at 11:44:44, nearly four hours before the
`resets_at` of 15:29:57 that its last real reading advertised:

```
11:05:52   98%, limit_id=codex,   resets_at 15:29:57
11:07:09   window-less, limit_id=premium      (a refused retry)
11:44:44    0%, limit_id=codex,   resets_at 16:44:31
```

**The owner redeemed a usage-reset voucher.** `resets_at` was not wrong --
the window really would have rolled at 15:29 -- and a fresh five-hour window
was granted at the moment of redemption, which is what 16:44:31 is. So the
spent reading's `resets_at` stays on the wire: it is the correct answer to
"when am I unblocked" in every case except one the account holder causes and
already knows about.

Worth stating because the opposite conclusion is very easy to reach from the
timestamps alone, and was: dropping the countdown on this evidence deletes a
true and useful number from the panel at the one moment it matters.

What is real here is the blind spot behind it. A voucher, a plan change, or
any other out-of-band refill is invisible to us: Codex writes a usage number
only when a turn SUCCEEDS, so between the refill and the next successful turn
the panel keeps showing the spent window. Nothing in the rollout announces it.

There is also no field to ask. `rate_limit_reached_type` and
`spend_control_reached` are carried in every one of these envelopes and look
exactly like the signal you would want; both were **null in all 342 envelopes
on this machine, the two exhausted ones included**. Do not reach for them.

## Confidence

One exhaustion event, on a `plus` plan, `limit_id` `codex` → `premium`. What
a different plan writes, and whether the weekly window nulls the same way
when *it* is the one spent, are both unobserved. The fix understates rather
than overstates when the signal is absent, which is the safe direction.

Raw evidence is kept outside this repo — it is 3.9 MB of session text. The
sanitised transition is `tests/fixtures/codex_rollout_limit_reached.jsonl`.
