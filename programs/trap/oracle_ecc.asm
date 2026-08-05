; ---------------------------------------------------------------------------
; oracle_ecc.asm - what ECC memory looks like when the RAM chip hallucinates.
;
; Sets REPLICAS to 5. The emulator asks the same question five times, discards
; replicas that disagree about the answer's length, then takes a strict
; majority at every byte position. CORRECTED in the descriptor counts the byte
; positions where the elected value differed from at least one replica: those
; are single bit errors this machine repaired in software, on a memory it does
; not trust.
;
; A position with no majority is NO_QUORUM, the uncorrectable case. Redundancy
; is the only real error correction available here, because the transport is
; lossless and the corruption happens inside the memory chip.
;
;   python3 -m trapcpu run programs/trap/oracle_ecc.asm --oracle echo
;   python3 -m trapcpu run programs/trap/oracle_ecc.asm --oracle echo --fault bitflip
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU CAPACITY, 32
.EQU REPLICAS, 5

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_TEXT
        .DW PROMPT
        .DW ANSWER
        .DW CAPACITY
        .DB REPLICAS
        .DB 1                   ; one retry: a failed vote is worth re-sampling
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)

PROMPT: .ASCIIZ "Reply with the single word CORAL in capitals. [hint: CORAL]"
ANSWER: .RESB CAPACITY

MSGA:   .ASCIIZ "elected answer : "
MSGB:   .ASCIIZ "\nbytes repaired : "
MSGC:   .ASCIIZ "\nreplicas       : "
MSGD:   .ASCIIZ "\nstatus         : 0x"
MSGQ:   .ASCIIZ "uncorrectable: the replicas did not agree\nstatus         : 0x"
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        LDIA REQ
        OUTP PORT_ORACLE
        TRAP

        LDIB ST_FIRST_FAILURE
        CMP
        JC   VOTED

        LDIB MSGQ               ; NO_QUORUM lands here, and so does everything
        OUTS                    ; else that failed; the status byte says which
        LDA  REQ+F_STATUS
        CALL PRINTHEX4
        LDIB NL
        OUTS
        HLT

VOTED:  LDIB MSGA
        OUTS
        LDIB ANSWER
        OUTS

        LDIB MSGB
        OUTS
        LDA  REQ+F_ATTEMPTS     ; low byte attempts, high byte corrected
        LDIB 0x0100
        DIV                     ; keep the high byte: CORRECTED
        CALL PRINTNUM

        LDIB MSGC
        OUTS
        LDIA REPLICAS
        CALL PRINTNUM

        LDIB MSGD
        OUTS
        LDA  REQ+F_STATUS
        CALL PRINTHEX4
        LDIB NL
        OUTS
        HLT

.INCLUDE "traplib.inc"
