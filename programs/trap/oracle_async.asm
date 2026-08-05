; ---------------------------------------------------------------------------
; oracle_async.asm - latency hiding: the CPU works while the oracle thinks.
;
; The synchronous TRAP freezes the whole machine for the entire round trip -
; fine when the peripheral is a memory chip, absurd when it is a language
; model that takes twenty seconds. TRAPA is the answer real hardware reached
; for: fire the request, keep executing, and take the result as an interrupt
; when it lands.
;
; This program fires one asynchronous oracle request, then spins a work loop
; that tallies how many units of useful work it completes *while the oracle is
; still thinking*. When the reply arrives the IRQ preempts the loop, the
; handler records the answer, and control returns via IRET to exactly where
; the work was interrupted. The printed work count is the latency that was
; hidden - cycles that a synchronous TRAP would have spent frozen.
;
;   python3 -m trapcpu run programs/trap/oracle_async.asm --oracle echo --latency 400
;   python3 -m trapcpu run programs/trap/oracle_async.asm --oracle echo --latency 0
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU CAP, 8

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_NUM
        .DW PROMPT
        .DW ANSWER
        .DW CAP
        .DB 1
        .DB 2
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)
PROMPT: .ASCIIZ "What is 6 times 7? Digits only. [hint: 42]"
ANSWER: .RESB CAP

WORK:   .DW 0                   ; work completed while the oracle was thinking
DONE:   .DW 0                   ; set by the interrupt handler

M_FIRE: .ASCIIZ "fired async request; CPU keeps running...\n"
M_IRQ:  .ASCIIZ "interrupt: oracle answered while we worked\n"
M_WORK: .ASCIIZ "work done during the wait: "
M_ANS:  .ASCIIZ " units\noracle's answer (6*7): "
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        LDIA HANDLER            ; install the completion interrupt vector
        OUTP PORT_IVEC
        STI                     ; unmask interrupts

        LDIA REQ
        OUTP PORT_ORACLE
        TRAPA                   ; fire and DO NOT wait

        LDIB M_FIRE
        OUTS

; --- useful work while the coprocessor is busy -----------------------------
; This loop runs entirely during the oracle's latency. Each iteration bumps a
; counter; the interrupt will preempt it the instant the reply is ready.
BUSY:
        LDA  WORK
        INC
        STA  WORK
        LDA  DONE
        JZ   BUSY               ; keep working until the handler flips DONE

; --- the reply has landed --------------------------------------------------
        LDIB M_WORK
        OUTS
        LDA  WORK
        CALL PRINTNUM
        LDIB M_ANS
        OUTS
        LDA  REQ+F_VALUE        ; the answer the handler left in the descriptor
        CALL PRINTNUM
        LDIB NL
        OUTS
        HLT

; --- interrupt handler: runs when the async request completes ---------------
; Registers must be preserved - the handler preempts the work loop at an
; arbitrary instruction boundary. IRET restores PC and the code segment and
; unmasks interrupts.
HANDLER:
        PUSH                    ; save A
        LDIB M_IRQ
        OUTS
        LDIA 1
        STA  DONE               ; tell the work loop to stop
        POP                     ; restore A
        IRET

.INCLUDE "traplib.inc"
