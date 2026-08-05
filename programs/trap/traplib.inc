; ---------------------------------------------------------------------------
; traplib.inc - string and number routines shared by the oracle demos.
;
; This emits code, so include it AFTER your program's last HLT. Its scratch
; variables live at 0x0200, below the 0x0300 area the demos use for their own
; data.
;
;   STRCPY     copy the NUL terminated string at [SRC] to [DST], advancing DST
;   NUMSTR     render A as decimal into NUMBUF; pointer left in NUMPTR
;   PRINTNUM   render A as decimal to the console
;   PRINTHEX4  render A as four hex digits to the console
; ---------------------------------------------------------------------------

.DATA
.ORG 0x0200

SRC:    .DW 0                   ; STRCPY source pointer
DST:    .DW 0                   ; STRCPY destination pointer, advanced in place
SCCHR:  .DW 0

NSVAL:  .DW 0
NSPTR:  .DW 0
NSDIG:  .DW 0
NUMPTR: .DW 0                   ; NUMSTR result pointer

HXVAL:  .DW 0
HXCNT:  .DW 0
HXDIG:  .DW 0

NUMBUF: .RESB 6
NUMEND: .DB 0                   ; digits are written backwards from here

.CODE

; --- STRCPY: [SRC] -> [DST], DST advanced past the copy ---------------------
STRCPY:
SC_LOOP:
        LDA  SRC
        MOVBA
        LDB                     ; A = RAM[SRC]; LDB sets Z on a NUL
        JZ   SC_END
        STA  SCCHR
        LDA  DST
        MOVBA
        LDA  SCCHR
        STB                     ; RAM[DST] = character
        LDA  SRC
        INC
        STA  SRC
        LDA  DST
        INC
        STA  DST
        JMP  SC_LOOP
SC_END:
        LDA  DST
        MOVBA
        LDIA 0
        STB                     ; keep the destination NUL terminated
        RET

; --- NUMSTR: A -> decimal string, pointer in NUMPTR ------------------------
NUMSTR:
        STA  NSVAL
        LDIA NUMEND
        STA  NSPTR
NS_LOOP:
        LDA  NSVAL
        LDIB 10
        DIV                     ; A = value / 10, D = value % 10
        STA  NSVAL
        MOVAD
        LDIB '0'
        ADD
        STA  NSDIG
        LDA  NSPTR
        DEC
        STA  NSPTR
        MOVBA
        LDA  NSDIG
        STB
        LDA  NSVAL
        JNZ  NS_LOOP            ; LDA set Z when the quotient reached zero
        LDA  NSPTR
        STA  NUMPTR
        RET

; --- PRINTNUM: A -> console, decimal ---------------------------------------
PRINTNUM:
        CALL NUMSTR
        LDA  NUMPTR
        MOVBA
        OUTS
        RET

; --- PRINTHEX4: A -> console, four hex digits ------------------------------
PRINTHEX4:
        STA  HXVAL
        LDIA 4
        STA  HXCNT
HX_LOOP:
        LDA  HXVAL
        LDIB 0x1000
        DIV                     ; A = top nibble, D = the rest
        STA  HXDIG
        MOVAD
        LDIB 16
        MUL                     ; shift the remainder up by one nibble
        STA  HXVAL
        LDA  HXDIG
        CALL HEXDIGIT
        LDA  HXCNT
        DEC
        STA  HXCNT
        JNZ  HX_LOOP
        RET

HEXDIGIT:
        LDIB 10
        CMP
        JC   HD_NUM             ; A < 10
        LDIB 55                 ; 'A' - 10
        ADD
        OUT
        RET
HD_NUM: LDIB '0'
        ADD
        OUT
        RET
