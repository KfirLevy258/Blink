#ifndef SLEEP_GATE_H
#define SLEEP_GATE_H

#include <stdbool.h>

/* The one rule for dozing (docs/sleep-mode-design.md): the host has gone
 * silent without saying goodbye, this boot has shown real figures at least
 * once (so a board that never met its app keeps saying "connecting"), and no
 * firmware update is in flight (the port is closed for ~75 s while esptool
 * writes -- silence that means the opposite of sleep). Pure, so the host
 * test in tests/sleep_gate can pin it. */
bool sleep_should_start(bool host_lost, bool had_usage, bool ota_busy);

#endif /* SLEEP_GATE_H */
