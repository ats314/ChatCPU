; ---------------------------------------------------------------------------
; oracle_num.asm - intelligence as an ALU operand.
;
; Asks the coprocessor for a number in NUM mode, then feeds it straight into
; MUL. The protocol guarantees D holds a validated 16-bit integer by the time
; the instruction after TRAP executes, so the multiply does not need to know
; that its operand was produced by a language model.
;
;   python3 -m trapcpu run programs/trap/oracle_num.asm --oracle echo
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU CAPACITY, 8

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_NUM            ; the emulator will reject anything non numeric
        .DW PROMPT
        .DW ANSWER
        .DW CAPACITY
        .DB 1                   ; replicas
        .DB 3                   ; retries: NUM mode is the easiest to violate
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)

PROMPT: .ASCIIZ "Pick your favourite integer between 2 and 200. Digits only. [hint: 137]"
ANSWER: .RESB CAPACITY

SECRET: .DW 0
SQUARE: .DW 0

MSG1:   .ASCIIZ "the oracle picked "
MSG2:   .ASCIIZ ", and "
MSG3:   .ASCIIZ " squared is "
MSG4:   .ASCIIZ "\nit took "
MSG5:   .ASCIIZ " attempt(s)\n"
BAD:    .ASCIIZ "oracle failed with status 0x"
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        LDIA REQ
        OUTP PORT_ORACLE
        TRAP

        LDIB ST_FIRST_FAILURE
        CMP
        JC   GOOD               ; CF means A < 2, so OK or DEGRADED

        LDIB BAD
        OUTS
        LDA  REQ+F_STATUS
        CALL PRINTHEX4
        LDIB NL
        OUTS
        HLT

GOOD:   MOVAD                   ; NUM mode leaves the validated value in D
        STA  SECRET
        MOVBA
        MUL                     ; A = value * value, CF set if it did not fit
        STA  SQUARE

        LDIB MSG1
        OUTS
        LDA  SECRET
        CALL PRINTNUM
        LDIB MSG2
        OUTS
        LDA  SECRET
        CALL PRINTNUM
        LDIB MSG3
        OUTS
        LDA  SQUARE
        CALL PRINTNUM

        LDIB MSG4
        OUTS
        LDA  REQ+F_ATTEMPTS     ; the descriptor records what the retry cost
        LDIB 0x00FF
        AND
        CALL PRINTNUM
        LDIB MSG5
        OUTS
        HLT

.INCLUDE "traplib.inc"
