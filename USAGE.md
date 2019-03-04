* create a directory that will contain all of the input files for the invokation
* put a compile command into `<dir>/compile.sh` (see e.g. `compile_example.sh`; it will be invoked as `./compile.sh input.c -o output.o`)
* `gcc -E -P -I header_dir -include <(echo '#define __attribute__(...)') orig_c_file.c > <dir>/source.c`
* `python3 strip_other_fns.py <dir>/source.c func_name`
* put asm for func_name into `<dir>/target.s`, with the following header:

```asm
.set noat
.set noreorder
.set gp=64
.macro glabel label
    .global \label
	.type \label, @function
    \label:
.endm
```
* `mips-linux-gnu-as -march=vr4300 -mabi=32 <dir>/target.s -o <dir>/target.o`
* optional sanity checks:
  - `./compile.sh <dir>/source.c -o <dir>/source.o`
  - `./diff.sh <dir>/target.o <dir>/source.o | colordiff | less`
* `python3 main_randomizer.py <dir>`