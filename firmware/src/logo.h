#ifndef LOGO_H
#define LOGO_H

#include "logo_parse.h"

/*
 * The company logo on this unit, or NULL for an individual unit.
 *
 * Read once from the `logo` partition and cached, the same way the edition
 * is: it is a factory fact, and the only consumer is the boot splash. The
 * blob lives in memory-mapped flash, so playing it costs the same nothing in
 * RAM as the compiled-in boot clips do -- on a board with ~3 KB of DRAM to
 * spare, that is the design rather than a nicety.
 */
const struct logo_info *logo_active(void);

#endif /* LOGO_H */
