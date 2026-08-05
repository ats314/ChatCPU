; ---------------------------------------------------------------------------
; trap.inc - TRAPCPU oracle ABI constants.
;
; Constants only; include this at the top of a program. The routines live in
; traplib.inc, which must be included after your last HLT because it emits code.
; ---------------------------------------------------------------------------

; --- I/O ports -------------------------------------------------------------
.EQU PORT_KEY,        0x00
.EQU PORT_KEY_STATE,  0x01
.EQU PORT_SCREEN,     0x10
.EQU PORT_RANDOM,     0x20
.EQU PORT_ORACLE,     0x30
.EQU PORT_ORACLE_STAT, 0x31

; --- descriptor ------------------------------------------------------------
.EQU ORACLE_MAGIC,    0x524F          ; 'O','R'
.EQU ORACLE_VERSION,  1
.EQU DESC_SIZE,       0x18

.EQU F_MAGIC,         0x00
.EQU F_VERSION,       0x02
.EQU F_MODE,          0x03
.EQU F_PROMPT,        0x04
.EQU F_RESPONSE,      0x06
.EQU F_CAPACITY,      0x08
.EQU F_REPLICAS,      0x0A
.EQU F_RETRIES,       0x0B
.EQU F_STATUS,        0x0C
.EQU F_LENGTH,        0x0E
.EQU F_NONCE,         0x10
.EQU F_ATTEMPTS,      0x12
.EQU F_CORRECTED,     0x13
.EQU F_FLAGS,         0x14
.EQU F_VALUE,         0x16

; --- modes -----------------------------------------------------------------
.EQU MODE_TEXT,       0
.EQU MODE_NUM,        1
.EQU MODE_BYTES,      2

; --- flags -----------------------------------------------------------------
.EQU FLAG_STRICT_CRC,     0x0001
.EQU FLAG_NO_TRIM,        0x0002
.EQU FLAG_ALLOW_TRUNCATE, 0x0004
.EQU FLAG_NO_NUL,         0x0008

; --- status codes ----------------------------------------------------------
.EQU ST_OK,             0x00
.EQU ST_DEGRADED,       0x01
.EQU ST_FIRST_FAILURE,  0x02          ; A < this means the trap succeeded
.EQU ST_NO_REQUEST,     0x10
.EQU ST_BAD_DESCRIPTOR, 0x11
.EQU ST_NO_FRAME,       0x12
.EQU ST_BAD_VERSION,    0x13
.EQU ST_BAD_NONCE,      0x14
.EQU ST_MALFORMED,      0x15
.EQU ST_BAD_LENGTH,     0x16
.EQU ST_BAD_CHECKSUM,   0x17
.EQU ST_OVERFLOW,       0x18
.EQU ST_BAD_ENCODING,   0x19
.EQU ST_NO_QUORUM,      0x1A
.EQU ST_REFUSED,        0x20
.EQU ST_RETRIES,        0x21
.EQU ST_BUDGET,         0x22
.EQU ST_ABORT,          0x23
