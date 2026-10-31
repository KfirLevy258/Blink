#include "edit_hold.h"

#include "msg_parse.h"
#include "theme.h"

static bool set_held(struct edit_hold *h, bool held)
{
	bool changed = h->held != held;

	h->held = held;
	return changed;
}

bool edit_hold_welcome(struct edit_hold *h, const char *json)
{
	double editing;

	h->host_tells = theme_caps_has(json, "edit_done");
	if (!h->host_tells) {
		/* Nobody on the other end will ever say the builder ended. */
		return set_held(h, false);
	}
	if (!msg_get_double(json, "editing", &editing)) {
		return false;	/* the probe's welcome: greet()'s will say */
	}
	return set_held(h, editing == 1);
}

bool edit_hold_reply(struct edit_hold *h, bool ok)
{
	if (ok && h->host_tells) {
		return set_held(h, true);
	}
	return false;
}

bool edit_hold_state(struct edit_hold *h, bool editing)
{
	if (!h->host_tells) {
		return false;
	}
	return set_held(h, editing);
}

bool edit_hold_done(struct edit_hold *h)
{
	return set_held(h, false);
}

bool edit_hold_host_gone(struct edit_hold *h)
{
	return set_held(h, false);
}

bool edit_hold_expired(struct edit_hold *h)
{
	return set_held(h, false);
}
