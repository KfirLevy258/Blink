#include <zephyr/kernel.h>
#include "proto.h"
#include "panel_probe.h"

int main(void)
{
	printk("[usage] firmware boot OK (uart-bridge)\n");
	panel_probe();
	proto_init();

	while (1) {
		proto_service();
		k_sleep(K_MSEC(10));
	}
	return 0;
}
