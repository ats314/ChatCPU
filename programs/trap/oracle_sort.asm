; ---------------------------------------------------------------------------
; oracle_sort.asm - a sort routine whose comparison operator is a model.
;
; Bubble sort is the most boring, most deterministic thing in computing. Every
; comparison is accounted for; the algorithm is provably correct; you can count
; the swaps on your fingers.
;
; This is that exact algorithm, with one substitution: where a normal sort does
;
;       CMP             ; flags from A - B
;
; this one does
;
;       OUTP PORT_ORACLE ; TRAP
;
; and asks which of two things is more dangerous. The control flow is ordinary
; 1970s assembly. The ordering relation is judgement. Nothing here can be
; sorted by a comparator - "a house fire" is not greater than "a wasp sting" in
; any arithmetic a CPU knows about - and yet the machine sorts it, because for
; this one instruction the ALU is a language model.
;
;   python3 -m trapcpu run programs/trap/oracle_sort.asm --oracle judge
;   python3 -m trapcpu run programs/trap/oracle_sort.asm --oracle claude
;   python3 -m trapcpu run programs/trap/oracle_sort.asm --oracle manual
;
; The 'judge' backend is a deterministic stand-in so the demo runs with no API
; key. It holds exactly one hardcoded opinion. The interesting run is 'claude',
; where the ordering is genuinely decided a question at a time.
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU N,      5                  ; how many items
.EQU NM1,    4                  ; N - 1, the loop bound
.EQU PBUFSZ, 160

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_NUM            ; the reply is a digit, parsed into F_VALUE
        .DW PBUF                ; prompt is built fresh for every comparison
        .DW ANSWER
        .DW 8
        .DB 1                   ; replicas
        .DB 2                   ; retries
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)

; The things to be sorted. Note that these are pointers: the sort moves 16-bit
; words around, exactly as it would for integers. It has no idea they are text.
S_PAPER: .ASCIIZ "a paper cut"
S_WASP:  .ASCIIZ "a wasp sting"
S_FIRE:  .ASCIIZ "a house fire"
S_CRASH: .ASCIIZ "a car crash"
S_HURR:  .ASCIIZ "a hurricane"

ITEMS:   .DW S_FIRE
         .DW S_PAPER
         .DW S_HURR
         .DW S_WASP
         .DW S_CRASH

QHEAD:  .ASCIIZ "Which of these is more dangerous? Reply with only the digit 1 or 2, nothing else.\n1) "
QMID:   .ASCIIZ "\n2) "

PBUF:   .RESB PBUFSZ            ; the prompt, rebuilt per comparison
ANSWER: .RESB 8

I:      .DW 0                   ; outer loop counter
J:      .DW 0                   ; inner loop counter
K:      .DW 0                   ; printing cursor
PA:     .DW 0                   ; left item under comparison
PB:     .DW 0                   ; right item
SLOT:   .DW 0                   ; &ITEMS[J]
NCMP:   .DW 0                   ; comparisons made

BANNER: .ASCIIZ "TRAPCPU: sorting by judgement, least dangerous first\n\n"
BEFORE: .ASCIIZ "before:\n"
AFTER:  .ASCIIZ "\nafter:\n"
INDENT: .ASCIIZ "  "
NL:     .ASCIIZ "\n"
TALLY:  .ASCIIZ "\nsorted in "
TAIL:   .ASCIIZ " oracle comparisons\n"
FAIL:   .ASCIIZ "oracle failed; status left in A\n"

; ---------------------------------------------------------------------------
.CODE

        LDIB BANNER
        OUTS
        LDIB BEFORE
        OUTS
        CALL PRINTLIST

        ; --- bubble sort ---------------------------------------------------
        LDIA 0
        STA  I
OUTER:
        LDIA 0
        STA  J
INNER:
        LDA  J                  ; SLOT = &ITEMS[J]
        CALL SLOTADR
        STA  SLOT

        MOVBA                   ; PA = ITEMS[J]
        LDW
        STA  PA

        LDA  SLOT               ; PB = ITEMS[J+1]
        INC
        INC
        MOVBA
        LDW
        STA  PB

        CALL ASKPAIR            ; A = 1 or 2, straight from the oracle

        LDIB 1                  ; a 1 means the LEFT item is more dangerous,
        CMP                     ; which puts it out of order for an ascending
        JNZ  NOSWAP             ; sort, so swap it down

        LDA  SLOT
        MOVBA
        LDA  PB
        STW                     ; ITEMS[J]   = PB
        LDA  SLOT
        INC
        INC
        MOVBA
        LDA  PA
        STW                     ; ITEMS[J+1] = PA
NOSWAP:
        LDA  J
        INC
        STA  J
        LDIB NM1
        CMP
        JC   INNER              ; CF set while J < N-1

        LDA  I
        INC
        STA  I
        LDIB NM1
        CMP
        JC   OUTER

        ; --- report --------------------------------------------------------
        LDIB AFTER
        OUTS
        CALL PRINTLIST
        LDIB TALLY
        OUTS
        LDA  NCMP
        CALL PRINTNUM
        LDIB TAIL
        OUTS
        HLT

; --- SLOTADR: A = index -> A = &ITEMS[index] -------------------------------
SLOTADR:
        MOVBA                   ; B = index
        LDIA 2
        MUL                     ; A = index * 2
        MOVBA
        LDIA ITEMS
        ADD                     ; A = ITEMS + index*2
        RET

; --- ASKPAIR: compare [PA] and [PB] via the coprocessor ---------------------
; Builds the prompt by concatenation, hands the descriptor to the device, and
; stops the whole machine until the answer is in RAM.
ASKPAIR:
        LDIA PBUF               ; STRCPY appends at DST and advances it
        STA  DST
        LDIA QHEAD
        STA  SRC
        CALL STRCPY
        LDA  PA
        STA  SRC
        CALL STRCPY
        LDIA QMID
        STA  SRC
        CALL STRCPY
        LDA  PB
        STA  SRC
        CALL STRCPY

        LDIA REQ
        OUTP PORT_ORACLE
        TRAP                    ; <- the machine stops here, every comparison

        LDIB ST_FIRST_FAILURE   ; success is any status below 0x02
        CMP
        JNC  APFAIL

        LDA  NCMP
        INC
        STA  NCMP
        LDA  REQ+F_VALUE
        RET
APFAIL:
        LDIB FAIL
        OUTS
        HLT

; --- PRINTLIST -------------------------------------------------------------
PRINTLIST:
        LDIA 0
        STA  K
PLOOP:
        LDIB INDENT
        OUTS
        LDA  K
        CALL SLOTADR
        MOVBA
        LDW                     ; A = ITEMS[K], a pointer
        MOVBA
        OUTS
        LDIB NL
        OUTS
        LDA  K
        INC
        STA  K
        LDIB N
        CMP
        JC   PLOOP
        RET

.INCLUDE "traplib.inc"
