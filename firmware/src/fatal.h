#ifndef FATAL_H
#define FATAL_H

/* The fatal-fault handler's two hooks into the rest of the firmware; the
 * handler itself is Zephyr's k_sys_fatal_error_handler, in fatal.c, and the
 * rules are in fatal_policy.h. */

/* Once, early in main(): read the reset cause and settle the fault streak. */
void fatal_boot(void);

/* From each mode's main loop, beside ota_boot_pump(): a boot that has been
 * up ten minutes wipes the streak. Cheap enough to call every pass. */
void fatal_pump(void);

#endif /* FATAL_H */
