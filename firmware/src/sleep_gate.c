#include "sleep_gate.h"

bool sleep_should_start(bool host_lost, bool had_usage, bool ota_busy)
{
	return host_lost && had_usage && !ota_busy;
}
