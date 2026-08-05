; ---------------------------------------------------------------------------
; oracle_jit.asm - the oracle writes machine code; the CPU runs it.
;
; This is the sharpest form of the whole idea. The model is asked, in BYTES
; mode, for a *routine* - actual TRAPCPU machine code. The bytes land in a
; scratch buffer (writable, non-executable). The guest cannot verify
; hallucinated code by reading it, so it does what real verified JITs do
; (eBPF, WASM): it restricts the code to a provably-safe subset and checks
; membership.
;
; The verifier is a linear sweep over an ALLOWLIST of one-byte, register-only
; opcodes - arithmetic, logic, register moves, and the RETX terminator. No
; memory writes, no I/O, no jumps, no traps. A routine built only from these
; is a pure function of the register file: it can compute from A/B/C/D and
; return, and it *cannot* escape, persist, or loop. Anything else is rejected
; before a single byte becomes executable.
;
; Only after the sweep passes does the guest copy the code onto page 7, raise
; PORT_MPROT to bless the page executable (which W^X makes simultaneously
; UNwritable), and CALLX into it. It then runs the routine on a known test
; vector and checks the answer in hardware before trusting it - property
; testing a hallucinated function inside its sandbox.
;
;   python3 -m trapcpu run programs/trap/oracle_jit.asm --oracle echo
;   python3 -m trapcpu run programs/trap/oracle_jit.asm --oracle 'echo'  # canonical
; ---------------------------------------------------------------------------

.INCLUDE "trap.inc"

.EQU CODEPAGE, 0x0700          ; where blessed code will live (page 7)
.EQU MAXCODE,  32
.EQU TESTIN,   7               ; run the routine on A = 7...
.EQU TESTOUT,  21              ; ...and demand A = 21 (times three)

; ---------------------------------------------------------------------------
.DATA
.ORG 0x0300

REQ:    .DW ORACLE_MAGIC
        .DB ORACLE_VERSION
        .DB MODE_BYTES         ; the reply is raw machine code
        .DW PROMPT
        .DW CODEBUF
        .DW MAXCODE
        .DB 1
        .DB 2
        .DW 0                   ; status    (out)
        .DW 0                   ; length    (out)
        .DW 0                   ; nonce     (out)
        .DB 0                   ; attempts  (out)
        .DB 0                   ; corrected (out)
        .DW 0                   ; flags
        .DW 0                   ; value     (out)

; The prompt spells out the exact safe ISA subset. [hint:] carries the
; canonical answer for the deterministic echo backend: MOVBA; ADD; ADD; RETX
; = 17 05 05 3F, which computes A <- 3*A.
PROMPT: .ASCIIZ "Write a TRAPCPU routine that multiplies register A by 3, leaving the result in A. Use ONLY these one-byte opcodes, hex, no spaces: MOVBA=17 ADD=05 SUB=06 INC=07 DEC=08 MOVAB=16 and terminate with RETX=3F. No other bytes are permitted. [hint:17 05 05 3F]"

CODEBUF: .RESB MAXCODE         ; scratch: DMA target, non-executable

; The allowlist: one-byte, register-only opcodes plus the RETX terminator.
; 0xFF terminates the table. Everything absent from this list is rejected.
ALLOW:  .DB 0x05, 0x06, 0x07, 0x08     ; ADD SUB INC DEC
        .DB 0x16, 0x17                 ; MOVAB MOVBA
        .DB 0x18, 0x19, 0x1A, 0x1B     ; ADDC ADDD SUBC SUBD
        .DB 0x1C, 0x1D, 0x1E, 0x1F     ; XOR AND OR NOT
        .DB 0x2B, 0x2C, 0x2D, 0x2E     ; MOVAC MOVCA MOVAD MOVDA
        .DB 0x33, 0x34, 0x35, 0x36     ; MUL DIV SHL SHR
        .DB 0x00                       ; NOP
        .DB 0x3F                       ; RETX
        .DB 0xFF                       ; table terminator

J_SCAN:   .DW 0                   ; verifier cursor into CODEBUF
J_LEFT:   .DW 0                   ; bytes remaining to verify
J_LAST:   .DW 0                   ; last opcode seen (must be RETX)
J_DST:    .DW 0                   ; copy cursor

M_ASK:  .ASCIIZ "asking the oracle for machine code...\n"
M_GOT:  .ASCIIZ "oracle returned "
M_BYTES: .ASCIIZ " bytes: "
M_VERI: .ASCIIZ "\nverifier: "
M_PASS: .ASCIIZ "PASS (all opcodes in the safe subset, ends in RETX)\n"
M_REJ:  .ASCIIZ "REJECT - "
M_BADOP: .ASCIIZ "forbidden opcode\n"
M_NORET: .ASCIIZ "does not end in RETX\n"
M_EMPTY: .ASCIIZ "empty or oracle failed\n"
M_BLESS: .ASCIIZ "blessed page 7 executable (now unwritable under W^X)\n"
M_RUN:  .ASCIIZ "CALLX result on A=7: "
M_TRUST: .ASCIIZ "  -> matches expected 21; code trusted\n"
M_WRONG: .ASCIIZ "  -> WRONG; the routine is well-formed but incorrect, discarded\n"
NL:     .ASCIIZ "\n"

; ---------------------------------------------------------------------------
.CODE

        LDIB M_ASK
        OUTS

        LDIA REQ
        OUTP PORT_ORACLE
        TRAP

        LDIB ST_FIRST_FAILURE
        CMP
        JC   GOTCODE
        LDIB M_REJ
        OUTS
        LDIB M_EMPTY
        OUTS
        HLT

GOTCODE:
        LDIB M_GOT
        OUTS
        LDA  REQ+F_LENGTH
        CALL PRINTNUM
        LDIB M_BYTES
        OUTS
        CALL DUMPHEX           ; show the raw bytes
        LDIB M_VERI
        OUTS

; --- the verifier: linear sweep over CODEBUF -------------------------------
        LDA  REQ+F_LENGTH
        STA  J_LEFT
        JZ   V_EMPTY
        LDIA CODEBUF
        STA  J_SCAN

V_LOOP:
        LDA  J_SCAN
        MOVBA
        LDB                    ; A = next opcode byte
        STA  J_LAST
        CALL ALLOWED           ; A nonzero if opcode is in the allowlist
        JZ   V_BADOP

        LDA  J_SCAN
        INC
        STA  J_SCAN
        LDA  J_LEFT
        DEC
        STA  J_LEFT
        JNZ  V_LOOP

; every byte allowlisted; the final one must be RETX (0x3F)
        LDA  J_LAST
        MOVBA
        LDIA 0x3F
        CMP
        JNZ  V_NORET

        LDIB M_PASS
        OUTS
        JMP  INSTALL

V_EMPTY:
        LDIB M_REJ
        OUTS
        LDIB M_EMPTY
        OUTS
        HLT
V_BADOP:
        LDIB M_REJ
        OUTS
        LDIB M_BADOP
        OUTS
        HLT
V_NORET:
        LDIB M_REJ
        OUTS
        LDIB M_NORET
        OUTS
        HLT

; --- install: copy to page 7, then bless it (W^X flips writable->exec) ------
INSTALL:
        LDIA CODEBUF
        STA  J_SCAN              ; reuse J_SCAN as source cursor
        LDIA CODEPAGE
        STA  J_DST
        LDA  REQ+F_LENGTH
        STA  J_LEFT

CP_LOOP:
        LDA  J_SCAN
        MOVBA
        LDB                    ; A = source byte
        MOVCA                  ; stash in C across the pointer juggling
        LDA  J_DST
        MOVBA
        MOVAC
        STB                    ; RAM[J_DST] = byte  (page 7 still writable here)
        LDA  J_SCAN
        INC
        STA  J_SCAN
        LDA  J_DST
        INC
        STA  J_DST
        LDA  J_LEFT
        DEC
        STA  J_LEFT
        JNZ  CP_LOOP

        LDIA 0x0701            ; (page 7 << 8) | PAGE_EXEC
        OUTP PORT_MPROT
        LDIB M_BLESS
        OUTS

; --- property-test the routine before trusting it --------------------------
        LDIA TESTIN
        CALLX CODEPAGE         ; run the oracle's code on A = 7
        STA  J_LEFT              ; reuse J_LEFT to hold the result
        LDIB M_RUN
        OUTS
        LDA  J_LEFT
        CALL PRINTNUM

        LDA  J_LEFT
        MOVBA
        LDIA TESTOUT
        CMP
        JZ   TRUSTED
        LDIB M_WRONG
        OUTS
        HLT
TRUSTED:
        LDIB M_TRUST
        OUTS
        HLT

; --- ALLOWED: A = opcode; returns A != 0 if it is in the allowlist ----------
ALLOWED:
        MOVCA                  ; C = target opcode
        LDIA ALLOW
        STA  J_DST               ; reuse J_DST as table cursor
AL_LOOP:
        LDA  J_DST
        MOVBA
        LDB                    ; A = table byte
        MOVDA                  ; D = table byte
        LDIB 0xFF
        CMP
        JZ   AL_NO             ; hit terminator -> not found
        MOVAD
        MOVBA
        MOVAC
        CMP                    ; A(target) - B(tableentry)
        JZ   AL_YES
        LDA  J_DST
        INC
        STA  J_DST
        JMP  AL_LOOP
AL_YES:
        LDIA 1
        RET
AL_NO:
        LDIA 0
        RET

; --- DUMPHEX: print REQ+F_LENGTH bytes at CODEBUF as spaced hex -------------
DUMPHEX:
        LDA  REQ+F_LENGTH
        STA  J_LEFT
        LDIA CODEBUF
        STA  J_SCAN
DH_LOOP:
        LDA  J_LEFT
        JZ   DH_END
        LDA  J_SCAN
        MOVBA
        LDB
        CALL PRINTHEX2
        LDIA ' '
        OUT
        LDA  J_SCAN
        INC
        STA  J_SCAN
        LDA  J_LEFT
        DEC
        STA  J_LEFT
        JMP  DH_LOOP
DH_END:
        RET

; --- PRINTHEX2: A -> two hex digits ----------------------------------------
PRINTHEX2:
        LDIB 16
        DIV                    ; A = high nibble, D = low nibble
        CALL HEX1
        MOVAD                  ; A = low nibble
        CALL HEX1
        RET
HEX1:
        LDIB 10
        CMP
        JC   HX_NUM
        LDIB 55
        ADD
        OUT
        RET
HX_NUM: LDIB '0'
        ADD
        OUT
        RET

.INCLUDE "traplib.inc"
