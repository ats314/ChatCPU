; ---------------------------------------------------------------------------
; oracle_guess.asm - a two player game where the second player is an opcode.
;
; The CPU picks a secret from the entropy port and then plays higher/lower
; against the coprocessor. Every round it assembles a fresh prompt in RAM out
; of string fragments and a rendered decimal, drops the pointer on port 0x30,
; and executes TRAP. The opponent's move arrives as a validated integer in D.
;
; This is the part that does not reduce to "call an API". The prompt is guest
; memory, built byte by byte with STB and INCB, and the reply is written back
; into guest memory before the next instruction retires. From the program's
; side there is no network and no model, only a slow register.
;
;   python3 -m trapcpu run programs/trap/oracle_guess.asm --oracle bisect
;   python3 -m trapcpu run programs/trap/oracle_guess.asm --oracle manual
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU CAPACITY,  8
.EQU MAXTRIES,  8
.EQU RANGE,     100

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_NUM
        .DW PROMPT              ; the prompt is built at runtime
        .DW ANSWER
        .DW CAPACITY
        .DB 1
        .DB 3
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)

ANSWER: .RESB CAPACITY

SECRET: .DW 0
GUESS:  .DW 0
TRIES:  .DW 0
FBPTR:  .DW 0                   ; pointer to this round's feedback fragment

PROMPT: .RESB 320               ; assembled fresh every round
FEEDB:  .RESB 160

BASE:   .ASCIIZ "We are playing higher or lower. I am thinking of a whole number from 1 to 100. Reply with your guess as digits only, nothing else."
FBNONE: .ASCIIZ " This is your first guess."
FBPRE:  .ASCIIZ " Your previous guess "
FBHIGH: .ASCIIZ " was too high."
FBLOW:  .ASCIIZ " was too low."
FBTAIL: .ASCIIZ " Guess again, digits only."

MSGG:   .ASCIIZ "oracle guesses "
MSGH:   .ASCIIZ " -> too high\n"
MSGL:   .ASCIIZ " -> too low\n"
MSGW:   .ASCIIZ " -> correct!\noracle won in "
MSGW2:  .ASCIIZ " guess(es)\n"
MSGX:   .ASCIIZ "oracle ran out of guesses; the secret was "
MSGE:   .ASCIIZ "oracle trap failed with status 0x"
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        INP  PORT_RANDOM        ; secret = 1 + (entropy mod RANGE)
        LDIB RANGE
        DIV
        MOVAD
        INC
        STA  SECRET

        LDIA 0
        STA  TRIES
        LDIA FBNONE
        STA  FBPTR

; --- one round -------------------------------------------------------------
ROUND:
        LDIA PROMPT             ; rebuild the prompt: BASE + feedback
        STA  DST
        LDIA BASE
        STA  SRC
        CALL STRCPY
        LDA  FBPTR
        STA  SRC
        CALL STRCPY

        LDIA REQ
        OUTP PORT_ORACLE
        TRAP                    ; <- the opponent moves here

        LDIB ST_FIRST_FAILURE
        CMP
        JC   MOVED

        LDIB MSGE
        OUTS
        LDA  REQ+F_STATUS
        CALL PRINTHEX4
        LDIB NL
        OUTS
        HLT

MOVED:  MOVAD                   ; D holds the validated guess
        STA  GUESS
        LDA  TRIES
        INC
        STA  TRIES

        LDIB MSGG
        OUTS
        LDA  GUESS
        CALL PRINTNUM

        LDA  SECRET
        MOVBA
        LDA  GUESS
        CMP                     ; flags and CF from guess - secret
        JZ   WIN
        JC   TOOLOW             ; CF means the guess borrowed, so guess < secret

        LDIB MSGH
        OUTS
        LDIA FBHIGH
        JMP  FEEDBACK

TOOLOW: LDIB MSGL
        OUTS
        LDIA FBLOW

; --- build " Your previous guess N was too X. Guess again..." ---------------
FEEDBACK:
        PUSH                    ; keep the high/low fragment across the copies
        LDIA FEEDB
        STA  DST
        LDIA FBPRE
        STA  SRC
        CALL STRCPY

        LDA  GUESS
        CALL NUMSTR
        LDA  NUMPTR
        STA  SRC
        CALL STRCPY

        POP                     ; the fragment chosen above
        STA  SRC
        CALL STRCPY

        LDIA FBTAIL
        STA  SRC
        CALL STRCPY

        LDIA FEEDB
        STA  FBPTR

        LDA  TRIES              ; out of guesses?
        MOVBA
        LDIA MAXTRIES
        CMP
        JZ   LOSE
        JMP  ROUND

; --- endings ---------------------------------------------------------------
WIN:    LDIB MSGW
        OUTS
        LDA  TRIES
        CALL PRINTNUM
        LDIB MSGW2
        OUTS
        HLT

LOSE:   LDIB MSGX
        OUTS
        LDA  SECRET
        CALL PRINTNUM
        LDIB NL
        OUTS
        HLT

.INCLUDE "traplib.inc"
