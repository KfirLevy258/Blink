#include <zephyr/kernel.h>
#include <lvgl.h>
#include <string.h>
#include <stdio.h>

#include "ui_setup.h"
#include "net_wifi.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)

static lv_obj_t *scr;
static lv_obj_t *status_lbl;

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

	lv_scr_load(scr);
}

void ui_setup_status(const char *msg)
{
	if (status_lbl) {
		lv_label_set_text(status_lbl, msg);
	}
}
