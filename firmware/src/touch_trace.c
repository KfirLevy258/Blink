/*
 * Touch trace -- a temporary diagnostic, not shipped code.
 *
 * Records every report the XPT2046 driver emits into a small RAM ring and
 * dumps the ring as CSV once the finger lifts. Built only when
 * CONFIG_CLAUGE_TOUCH_TRACE=y (see trace.conf); absent from release builds.
 *
 * Why RAM-then-dump instead of the driver's own LOG_DBG: with reads=<1> the
 * driver free-runs at a few hundred reports per second, while a ~30-char log
 * line at 115200 baud takes ~2.6 ms to drain. Logging from the hot path would
 * back up and drop exactly the samples this exists to measure -- the handful
 * taken during the touch-down settling transient.
 *
 * The hook is on the xpt2046 device itself, so coordinates are the driver's
 * output in the CHANNEL frame (portrait, 240x320) -- before lvgl_pointer's
 * swap/invert/rotate. That is deliberate: it is the same point in the chain
 * where a filter would sit, so the numbers measured here are the numbers that
 * filter would be tuned with.
 */
#include <zephyr/kernel.h>
#include <zephyr/input/input.h>
#include <zephyr/sys/printk.h>
#include <zephyr/init.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/gpio.h>

/* 128 x 12 B = 1536 B. DRAM is tight on this board (dram1 is the binding
 * segment), which is the other reason the ring is this size and the dump is
 * plain printk rather than a second thread with its own stack. */
#define TT_CAP 128

struct tt_sample {
	uint32_t us;	/* since the press-down edge */
	int16_t x;
	int16_t y;
	uint8_t down;
	uint8_t pad;
};

static struct tt_sample tt_buf[TT_CAP];
static uint32_t tt_t0;
static uint16_t tt_n;
static bool tt_active;
static bool tt_saturated;
static uint32_t tt_seq;

/* Latest field values; the driver sends ABS_X and ABS_Y with sync=0 and closes
 * the report with BTN_TOUCH sync=1, so a report is complete on the sync. */
static int32_t cur_x, cur_y;
static int32_t cur_down;

/* Proof-of-life. Three captures produced no output at all, which cannot
 * distinguish "nobody touched the screen" from "the tracer never ran" from
 * "the driver never reported a press". These counters plus the heartbeat below
 * separate all three: no heartbeat at all means the tracer is not running;
 * heartbeat with ev=0 means the driver is silent; ev>0 with press=0 means it
 * reports without ever asserting BTN_TOUCH. */
static uint32_t n_ev, n_sync, n_press;

static void tt_alive(struct k_work *work)
{
	struct k_work_delayable *dw = k_work_delayable_from_work(work);

	printk("#TT-ALIVE ev=%u sync=%u press=%u\n", n_ev, n_sync, n_press);
	k_work_reschedule(dw, K_SECONDS(5));
}
static K_WORK_DELAYABLE_DEFINE(tt_alive_work, tt_alive);


/* ---- raw panel probe -------------------------------------------------
 *
 * Talks to the XPT2046 directly, alongside the driver, so panel health can be
 * judged WITHOUT anyone touching the screen. Five capture windows produced
 * ev=0, which cannot distinguish "nobody tapped" from "the panel is not
 * responding" -- this can.
 *
 * Reading it:
 *   z1/z2 pinned at 0 or 4095, x/y likewise  -> SPI or wiring is dead
 *   plausible values, penirq=1 (inactive)    -> panel healthy, simply untouched
 *   penirq=0 (asserted) but the driver silent -> interrupt/threshold problem
 *
 * The command sequence mirrors the driver's: Z1, Z2, X with the reference
 * powered, then Y with POWER_OFF to re-enable PENIRQ. Zephyr's SPI API locks
 * per transaction, so sharing the bus with the driver is safe.
 */
#define PR_START	BIT(7)
#define PR_CH(ch)	(((ch) & 0x7) << 4)
#define PR_ON		0x03
#define PR_OFF		0
#define PR_U16(b, i)	((uint16_t)(((b)[i] & 0x7f) << 5) | ((b)[i + 1] >> 3))

static const struct spi_dt_spec pr_bus = SPI_DT_SPEC_GET(
	DT_NODELABEL(xpt2046),
	SPI_OP_MODE_MASTER | SPI_TRANSFER_MSB | SPI_WORD_SET(8), 0);
static const struct gpio_dt_spec pr_int =
	GPIO_DT_SPEC_GET(DT_NODELABEL(xpt2046), int_gpios);

static uint8_t pr_tx[9] = {
	[0] = PR_START | PR_CH(3) | PR_ON,	/* CH_Z1 */
	[2] = PR_START | PR_CH(4) | PR_ON,	/* CH_Z2 */
	[4] = PR_START | PR_CH(5) | PR_ON,	/* CH_X  */
	[6] = PR_START | PR_CH(1) | PR_OFF,	/* CH_Y  */
};

static void tt_probe(struct k_work *work)
{
	struct k_work_delayable *dw = k_work_delayable_from_work(work);
	uint8_t rx[9] = {0};
	const struct spi_buf txb = {.buf = pr_tx, .len = sizeof(pr_tx)};
	const struct spi_buf rxb = {.buf = rx, .len = sizeof(rx)};
	const struct spi_buf_set txs = {.buffers = &txb, .count = 1};
	const struct spi_buf_set rxs = {.buffers = &rxb, .count = 1};

	int ret = spi_transceive_dt(&pr_bus, &txs, &rxs);
	int irq = gpio_pin_get_dt(&pr_int);

	if (ret < 0) {
		printk("#TT-PROBE spi_err=%d penirq=%d\n", ret, irq);
	} else {
		uint16_t z1 = PR_U16(rx, 1), z2 = PR_U16(rx, 3);

		printk("#TT-PROBE penirq=%d z1=%u z2=%u x=%u y=%u z=%d\n",
		       irq, z1, z2, PR_U16(rx, 5), PR_U16(rx, 7),
		       (int)z1 + 4096 - (int)z2);
	}
	k_work_reschedule(dw, K_SECONDS(2));
}
static K_WORK_DELAYABLE_DEFINE(tt_probe_work, tt_probe);

static int tt_start_heartbeat(void)
{
	k_work_reschedule(&tt_alive_work, K_SECONDS(5));
	k_work_reschedule(&tt_probe_work, K_SECONDS(6));
	return 0;
}
SYS_INIT(tt_start_heartbeat, APPLICATION, 99);

static void tt_dump(void)
{
	/* Runs on the input thread after the release, when nothing is waiting
	 * on it. ~128 lines at 115200 is ~0.4 s; a second tap during the dump
	 * would queue behind it (queue depth 96) rather than be lost. */
	printk("\n#TT-BEGIN seq=%u n=%u sat=%u frame=channel240x320\n",
	       tt_seq, tt_n, tt_saturated ? 1U : 0U);
	printk("#TT i,us,x,y,down\n");
	for (uint16_t i = 0; i < tt_n; i++) {
		printk("#TT %u,%u,%d,%d,%u\n", i, tt_buf[i].us,
		       tt_buf[i].x, tt_buf[i].y, tt_buf[i].down);
	}
	printk("#TT-END seq=%u\n", tt_seq);
	tt_seq++;
}

static void tt_cb(struct input_event *evt, void *user_data)
{
	ARG_UNUSED(user_data);

	n_ev++;

	switch (evt->code) {
	case INPUT_ABS_X:
		cur_x = evt->value;
		break;
	case INPUT_ABS_Y:
		cur_y = evt->value;
		break;
	case INPUT_BTN_TOUCH:
		cur_down = evt->value;
		break;
	default:
		break;
	}

	if (!evt->sync) {
		return;
	}

	n_sync++;
	if (cur_down) {
		n_press++;
	}

	uint32_t now = k_cycle_get_32();

	if (cur_down && !tt_active) {	/* press-down edge: start a trace */
		tt_active = true;
		tt_saturated = false;
		tt_n = 0;
		tt_t0 = now;
	}

	if (!tt_active) {
		return;			/* stray release with no press */
	}

	if (tt_n < TT_CAP) {
		/* 32-bit cycle counter wraps every ~17.9 s at 240 MHz; the
		 * unsigned subtraction stays correct across one wrap, which is
		 * far longer than any touch. */
		tt_buf[tt_n].us = k_cyc_to_us_floor32(now - tt_t0);
		tt_buf[tt_n].x = (int16_t)cur_x;
		tt_buf[tt_n].y = (int16_t)cur_y;
		tt_buf[tt_n].down = (uint8_t)(cur_down ? 1 : 0);
		tt_n++;
	} else {
		tt_saturated = true;
	}

	if (!cur_down) {		/* release closes the trace */
		tt_active = false;
		tt_dump();
	}
}

#if DT_NODE_EXISTS(DT_NODELABEL(xpt2046))
INPUT_CALLBACK_DEFINE(DEVICE_DT_GET(DT_NODELABEL(xpt2046)), tt_cb, NULL);
#else
#error "touch trace needs the xpt2046 node"
#endif
