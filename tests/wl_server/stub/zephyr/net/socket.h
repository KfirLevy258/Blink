#pragma once
#include <sys/types.h>
#include <stdint.h>
#include <stddef.h>
#include "zephyr/net/net_ip.h"
#define SOCK_STREAM 1
#define SOCK_DGRAM 2
#define IPPROTO_TCP 6
#define IPPROTO_UDP 17
#define SOL_SOCKET 1
#define SO_SNDTIMEO 21
#define TCP_NODELAY 1
#define ZSOCK_MSG_DONTWAIT 0x40
#define ZSOCK_POLLIN 1
#define ZSOCK_POLLHUP 2
#define ZSOCK_POLLERR 4
struct zsock_timeval { long tv_sec; long tv_usec; };
struct zsock_pollfd { int fd; short events; short revents; };
int zsock_socket(int af, int type, int proto);
int zsock_bind(int fd, const struct sockaddr *a, socklen_t l);
int zsock_listen(int fd, int backlog);
int zsock_accept(int fd, struct sockaddr *a, socklen_t *l);
int zsock_close(int fd);
int zsock_setsockopt(int fd, int lvl, int opt, const void *v, socklen_t l);
ssize_t zsock_send(int fd, const void *p, size_t n, int flags);
ssize_t zsock_recv(int fd, void *p, size_t n, int flags);
ssize_t zsock_recvfrom(int fd, void *p, size_t n, int flags,
		       struct sockaddr *from, socklen_t *fl);
ssize_t zsock_sendto(int fd, const void *p, size_t n, int flags,
		     const struct sockaddr *to, socklen_t l);
int zsock_poll(struct zsock_pollfd *p, int n, int timeout);
