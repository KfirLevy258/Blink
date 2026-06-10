#include <zephyr/kernel.h>
#include "proto.h"

int main(void)
{
	printk("[usage] firmware boot OK (uart-bridge)\n");
	proto_init();

	while (1) {
		proto_service();
		k_sleep(K_MSEC(20));
	}
	return 0;
}
