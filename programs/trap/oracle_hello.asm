; ---------------------------------------------------------------------------
; oracle_hello.asm - the smallest program that calls a frontier model
;
;   LDIA REQ ; OUTP 0x30 ; TRAP
;
; Three instructions. The machine stops at TRAP, the request frame goes into
; the conversation, and whatever came back is already in RAM by the time the
; fourth instruction runs. A 6502 called a floating point chip like this.
;
;   python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle echo
;   python3 -m trapcpu run programs/trap/oracle_hello.asm --oracle manual
; ---------------------------------------------------------------------------

.EQU ORACLE,    0x30
.EQU MAGIC,     0x524F
.EQU VERSION,   1
.EQU MODE_TEXT, 0
.EQU CAPACITY,  48

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW MAGIC               ; +0x00 magic 'OR'
        .DB VERSION             ; +0x02 protocol version
        .DB MODE_TEXT           ; +0x03 mode
        .DW PROMPT              ; +0x04 prompt pointer
        .DW ANSWER              ; +0x06 response buffer
        .DW CAPACITY            ; +0x08 response capacity
        .DB 1                   ; +0x0A replicas
        .DB 2                   ; +0x0B retries
        .DW 0                   ; +0x0C status     (out)
        .DW 0                   ; +0x0E length     (out)
        .DW 0                   ; +0x10 nonce      (out)
        .DB 0                   ; +0x12 attempts   (out)
        .DB 0                   ; +0x13 corrected  (out)
        .DW 0                   ; +0x14 flags
        .DW 0                   ; +0x16 value      (out)

PROMPT: .ASCIIZ "Greet a 16-bit computer in under 40 characters. [hint: HELLO FROM THE OTHER SIDE OF THE BUS]"
ANSWER: .RESB CAPACITY

BANNER: .ASCIIZ "TRAPCPU asks the oracle...\n"
GOTIT:  .ASCIIZ "oracle says: "
OOPS:   .ASCIIZ "oracle failed; see the status byte in A\n"
NEWLIN: .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        LDIB BANNER
        OUTS

        LDIA REQ                ; hand the descriptor to the coprocessor
        OUTP ORACLE
        TRAP                    ; <- the machine stops here

        ; A now holds the completion status. Success is OK (0) or DEGRADED (1),
        ; so the test is "A < 2": CMP sets CF when A - B borrows.
        LDIB 2
        CMP
        JC   GOOD

        LDIB OOPS
        OUTS
        HLT

GOOD:   LDIB GOTIT
        OUTS
        LDIB ANSWER
        OUTS
        LDIB NEWLIN
        OUTS
        HLT
