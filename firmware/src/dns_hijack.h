#ifndef DNS_HIJACK_H
#define DNS_HIJACK_H

/* Answer every DNS lookup with the board's own AP address, so the phone's
 * captive-portal probe lands on us and the setup page opens by itself.
 */
void dns_hijack_start(void);
void dns_hijack_stop(void);

#endif /* DNS_HIJACK_H */
