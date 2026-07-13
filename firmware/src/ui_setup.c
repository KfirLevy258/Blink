#include <zephyr/kernel.h>
#include <lvgl.h>
#include <string.h>
#include <stdio.h>
#include <zephyr/kernel.h>

#include "ui_setup.h"
#include "net_wifi.h"
#include "portal.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)

static lv_obj_t *scr;
static lv_obj_t *status_lbl;
static volatile int pending_sta = -1;   /* set from net-mgmt ctx, applied in the LVGL loop */

/* Runs in the network management context, NOT the LVGL thread -- so it only
 * records the value; ui_setup_service() applies it where LVGL is safe to touch.
 */
static void on_station(int count)
{
	pending_sta = count;
}

void ui_setup_show(void)
{
	scr = lv_obj_create(NULL);
	lv_obj_set_style_bg_color(scr, COL_BG, 0);
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

	lv_obj_t *t = lv_label_create(scr);

	lv_label_set_text(t, "SETUP");
	lv_obj_set_style_text_color(t, COL_GREEN, 0);
	lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
	lv_obj_align(t, LV_ALIGN_TOP_LEFT, 12, 10);

	lv_obj_t *l1 = lv_label_create(scr);
	static char txt[160];

	snprintf(txt, sizeof(txt),
		 "Scan with your phone\nto join and set up.\n\n"
		 "Or join WiFi:\n%s\n\nthen open\nhttp://%s",
		 net_wifi_ap_ssid(), AP_IP);
	lv_label_set_text(l1, txt);
	lv_obj_set_style_text_color(l1, COL_TEXT, 0);
	lv_obj_align(l1, LV_ALIGN_TOP_LEFT, 12, 44);

	/* A WIFI: QR, so one scan joins the AP. The captive portal then opens the
	 * page on its own -- the user never types an SSID or an IP.
	 */
	const char *qr_payload = net_wifi_ap_qr();
	lv_obj_t *qr = lv_qrcode_create(scr);

	lv_qrcode_set_size(qr, 118);
	lv_qrcode_set_dark_color(qr, lv_color_black());
	lv_qrcode_set_light_color(qr, lv_color_white());
	lv_qrcode_update(qr, qr_payload, strlen(qr_payload));
	lv_obj_align(qr, LV_ALIGN_TOP_RIGHT, -14, 54);

	status_lbl = lv_label_create(scr);
	lv_label_set_text(status_lbl, "waiting for setup...");
	lv_obj_set_style_text_color(status_lbl, COL_DIM, 0);
	lv_obj_align(status_lbl, LV_ALIGN_BOTTOM_MID, 0, -8);

	net_wifi_set_sta_cb(on_station);
	lv_scr_load(scr);
}

void ui_setup_status(const char *msg)
{
	if (status_lbl) {
		lv_label_set_text(status_lbl, msg);
	}
}

/* Call from the main LVGL loop. Reflects station join/leave onto the screen so
 * setup can be observed without a serial cable (which would reset the board).
 */
void ui_setup_service(void)
{
	static int shown = -1;
	int c = pending_sta;

	if (c >= 0) {
		pending_sta = -1;
		shown = c;
	}
	if (shown <= 0 || !status_lbl) {
		return;
	}
	/* Refresh at most ~2x/sec so the http counter updates live without
	 * thrashing the label every poll.
	 */
	static int64_t last;
	int64_t now = k_uptime_get();

	if (now - last < 500) {
		return;
	}
	last = now;
	c = shown;
	if (c > 0) {
		static char msg[80];

		snprintf(msg, sizeof(msg),
			 "http:%d rx:%d tx:%d\nopen http://192.168.4.1 in Safari",
			 portal_conn_count(), portal_last_rx(), portal_last_tx());
		lv_label_set_text(status_lbl, msg);
		lv_obj_set_style_text_color(status_lbl, COL_GREEN, 0);
	} else {
		lv_label_set_text(status_lbl, "waiting for phone to join...");
		lv_obj_set_style_text_color(status_lbl, COL_DIM, 0);
	}
}
