/*
 * H4 bridge: HCI packets between UART0 (the board's USB-serial chip) and the controller's VHCI.
 */
#pragma once

#include <stdint.h>

/* Vendor commands answered by the bridge itself (OGF 0x3F, OCF 0x07C..0x07E: below Espressif's
 * own vendor opcodes, which start at OCF 0x080, so they never reach the controller). */
#define LXBT_OP_STATS    0xFC7C   /* -> status, 8 x u32 counters (see dongle_stats_t) */
#define LXBT_OP_SET_BAUD 0xFC7D   /* u32 baud -> status; the new rate applies after the reply */
#define LXBT_OP_IDENTIFY 0xFC7E   /* -> status, "LXBT", proto, fw x.y.z, u32 baud, flags, max ACL, max LE */

#define LXBT_PROTO_VERSION 1

typedef struct {
    uint32_t rx_bytes;      /* bytes from the host */
    uint32_t tx_bytes;      /* bytes to the host */
    uint32_t rx_pkts;       /* packets from the host passed to the controller (or answered here) */
    uint32_t tx_pkts;       /* packets from the controller sent to the host */
    uint32_t resync_bytes;  /* bytes from the host thrown away (no valid H4 packet start) */
    uint32_t dropped_pkts;  /* packets from the host dropped (too long, or cut short by a gap) */
    uint32_t rx_overflow;   /* UART FIFO / ring buffer overflows: the host sent faster than we read */
    uint32_t rx_errors;     /* UART frame / parity / break errors: baud too high or a bad cable */
} dongle_stats_t;

/* Starts the bridge task (it installs the UART driver on its own core). Call after the controller is
 * enabled. */
void h4_bridge_start(void);
