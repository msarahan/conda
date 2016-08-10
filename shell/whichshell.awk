#!/bin/awk -f
BEGIN {}
{
    # detect the clause/mode
    clause=substr($NF,1,1)
    if (clause == "e") {
        # error clause
        clause=1
    } else if (clause == "b") {
        # bourne shell clause
        clause=2
    } else if (clause == "c") {
        # c-shell clause
        clause=3
    } else {
        # bad clause
        exit(2)
    }

    # parse parameters
    # expect to recieve in this order:
    #  1) "`ps -p $$ | sed 's|.*csh|THIS-IS-CSH|' | grep CSH || echo $0`"
    #  2) "`ps -p $$ | tr '\n' ' '`"
    #  3) "`test -x /usr/bin/lsb_release && lsb_release -si || uname -s`"
    #  4) "$SHLVL"
    #  5) script's name to compare $0 against
    #  6) what clause/mode (detected above)
    #
    # We parse based on knowing that the second parameter (ps -p $$)
    # always starts with "PID".
    # We also know that in certain cases, if the script is being
    # executed the second paramter will contain more than 2
    # parameters and hence we will always expect a valid parse to
    # only contain 6 parameters.
    num=split(substr($0,index($0,"PID")),a," ")
    if (num != 12) {
        exit(clause != 1)
    }
    # a[1] == "PID"
    # a[2] == "TTY"
    # a[3] == "TIME"
    # a[4] == "CMD"
    # a[5] == pid
    # a[6] == tty
    # a[7] == time
    sh="."a[8]
    sy=a[9]
    sl=a[10]
    sn=".*"a[11]".*"
    # a[12] == $NF == clause
    sz=substr($0,1,index($0,"PID")-1)

    # match shells:
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".bash")                                ||
         (sh == ".-bash")                               ||
         (sh == "./bin/bash")                           ||
         (sh == ".-bin/bash"))) {
        exit(clause != 2)
    }
    # when sourcing zsh $0 looks the same as executing
    # zsh being sourced vs executed is detected via the number of parameters parsed above
    if ((sh == ".zsh")                                  ||
        (sh == ".-zsh")                                 ||
        (sh == "./bin/zsh")                             ||
        (sh == ".-bin/zsh")) {
        exit(clause != 2)
    }
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".dash")                                ||
         (sh == ".-dash")                               ||
         (sh == "./bin/dash")                           ||
         (sh == ".-/bin/dash")                          ||
         (sh == ".sh" && sy == "Ubuntu"))) {
        exit(clause != 2)
    }
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".posh")                                ||
         (sh == ".-posh")                               ||
         (sh == "./bin/posh")                           ||
         (sh == ".-/bin/posh"))) {
        exit(clause != 2)
    }
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".sh" && sy != "Ubuntu")                ||
         (sh == ".-sh" && sy == "Darwin" && sl == "1")  ||
         (sh == ".-sh" && sy == "Linux")                ||
         (sh == "./bin/sh")                             ||
         (sh == ".-bin/sh"))) {
        exit(clause != 2)
    }
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".csh")                                 ||
         (sh == ".-csh" && sy == "Darwin" && sl == "1") ||
         (sh == ".-sh"  && sy == "Darwin" && sl != "1") ||
         (sh == ".-csh" && sy == "Linux")               ||
         (sh == "./bin/csh")                            ||
         (sh == ".-bin/csh"))) {
        exit(clause != 3)
    }
    if ((match(sz,sn) == 0)                             &&
        ((sh == ".tcsh")                                ||
         (sh == ".-tcsh" && sy == "Darwin" && sl == "1")||
         (sh == ".-csh"  && sy == "Darwin" && sl != "1")||
         (sh == ".-tcsh" && sy == "Linux")              ||
         (sh == "./bin/tcsh")                           ||
         (sh == ".-bin/tcsh"))) {
        exit(clause != 3)
    }
    exit(clause != 1)
}
END {}
