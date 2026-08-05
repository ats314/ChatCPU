; ---------------------------------------------------------------------------
; oracle_quiz.asm - five traps, one per protocol condition.
;
; Written for a live run against a real model over frame/resume. Each trap
; exercises a different part of the contract:
;
;   Q1  TEXT           plain answer, no checksum demanded -> expect DEGRADED
;   Q2  NUM            arithmetic the CPU re-verifies with MUL
;   Q3  BYTES          hex pairs decoded straight into RAM
;   Q4  TEXT + STRICT  a checksum is REQUIRED; only a tool-using model passes
;   Q5  TEXT x3        replica vote on a factual answer
;
; Q2 is the point of the exercise: the guest program checks the oracle's
; answer in hardware. "Ask a question whose answer you can verify" is the
; only defense against a confidently wrong oracle, and it is spelled MUL.
;
;   python3 -m trapcpu frame programs/trap/oracle_quiz.asm --state quiz.disk
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ1:   .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_TEXT
        .DW PR1
        .DW ANS1
        .DW 24
        .DB 1
        .DB 1
        .DW 0
        .DW 0
        .DW 0
        .DB 0
        .DB 0
        .DW 0
        .DW 0
PR1:    .ASCIIZ "What is the capital of Denmark? Reply with the city name only."
ANS1:   .RESB 24

REQ2:   .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_NUM
        .DW PR2
        .DW ANS2
        .DW 8
        .DB 1
        .DB 2
        .DW 0
        .DW 0
        .DW 0
        .DB 0
        .DB 0
        .DW 0
        .DW 0
PR2:    .ASCIIZ "What is 13 multiplied by 17? Reply with the digits only."
ANS2:   .RESB 8

REQ3:   .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_BYTES
        .DW PR3
        .DW ANS3
        .DW 8
        .DB 1
        .DB 2
        .DW 0
        .DW 0
        .DW 0
        .DB 0
        .DB 0
        .DW 0
        .DW 0
PR3:    .ASCIIZ "Hex-encode the ASCII bytes of the word TRAP, uppercase hex digits only."
ANS3:   .RESB 8

REQ4:   .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_TEXT
        .DW PR4
        .DW ANS4
        .DW 32
        .DB 1
        .DB 1
        .DW 0
        .DW 0
        .DW 0
        .DB 0
        .DB 0
        .DW FLAG_STRICT_CRC
        .DW 0
PR4:    .ASCIIZ "Reply with exactly this text: THE MODEL IS THE PERIPHERAL"
ANS4:   .RESB 32

REQ5:   .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_TEXT
        .DW PR5
        .DW ANS5
        .DW 8
        .DB 3                   ; three independent replicas
        .DB 1
        .DW 0
        .DW 0
        .DW 0
        .DB 0
        .DB 0
        .DW 0
        .DW 0
PR5:    .ASCIIZ "What is the chemical symbol for gold? Reply with the symbol only."
ANS5:   .RESB 8

SQ1:    .ASCIIZ "Q1 TEXT   status "
SQ2:    .ASCIIZ "Q2 NUM    status "
SQ3:    .ASCIIZ "Q3 BYTES  status "
SQ4:    .ASCIIZ "Q4 STRICT status "
SQ5:    .ASCIIZ "Q5 VOTE   status "
ARROW:  .ASCIIZ " -> "
SVOK:   .ASCIIZ "   CPU check: 13*17 verified in hardware, MATCH\n"
SVBAD:  .ASCIIZ "   CPU check: MISMATCH - the oracle got the arithmetic wrong\n"
SCORR:  .ASCIIZ "   bytes repaired by vote: "
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

; --- Q1: plain text ---------------------------------------------------------
        LDIA REQ1
        OUTP PORT_ORACLE
        TRAP
        LDIB SQ1
        OUTS
        LDA  REQ1+F_STATUS
        CALL PRINTHEX4
        LDIB ARROW
        OUTS
        LDIB ANS1
        OUTS
        LDIB NL
        OUTS

; --- Q2: a number the CPU re-verifies --------------------------------------
        LDIA REQ2
        OUTP PORT_ORACLE
        TRAP
        LDIB SQ2
        OUTS
        LDA  REQ2+F_STATUS
        CALL PRINTHEX4
        LDIB ARROW
        OUTS
        LDA  REQ2+F_VALUE
        CALL PRINTNUM
        LDIB NL
        OUTS

        LDIA 13
        LDIB 17
        MUL                     ; A = 221, computed by silicon
        MOVBA                   ; B = 221
        LDA  REQ2+F_VALUE       ; A = whatever the model said
        CMP
        JZ   Q2OK
        LDIB SVBAD
        OUTS
        JMP  Q3GO
Q2OK:   LDIB SVOK
        OUTS

; --- Q3: raw bytes ----------------------------------------------------------
Q3GO:   LDIA REQ3
        OUTP PORT_ORACLE
        TRAP
        LDIB SQ3
        OUTS
        LDA  REQ3+F_STATUS
        CALL PRINTHEX4
        LDIB ARROW
        OUTS
        LDIB ANS3               ; the decoded bytes spell TRAP in ASCII
        OUTS
        LDIB NL
        OUTS

; --- Q4: checksum REQUIRED --------------------------------------------------
        LDIA REQ4
        OUTP PORT_ORACLE
        TRAP
        LDIB SQ4
        OUTS
        LDA  REQ4+F_STATUS
        CALL PRINTHEX4
        LDIB ARROW
        OUTS
        LDIB ANS4
        OUTS
        LDIB NL
        OUTS

; --- Q5: replica vote -------------------------------------------------------
        LDIA REQ5
        OUTP PORT_ORACLE
        TRAP
        LDIB SQ5
        OUTS
        LDA  REQ5+F_STATUS
        CALL PRINTHEX4
        LDIB ARROW
        OUTS
        LDIB ANS5
        OUTS
        LDIB NL
        OUTS
        LDIB SCORR
        OUTS
        LDA  REQ5+F_ATTEMPTS    ; low byte attempts, high byte corrected
        LDIB 0x0100
        DIV                     ; keep the high byte
        CALL PRINTNUM
        LDIB NL
        OUTS

        HLT

.INCLUDE "traplib.inc"
