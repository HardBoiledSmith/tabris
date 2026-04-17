# .bashrc

# Source global definitions
if [ -f /etc/bashrc ]; then
	. /etc/bashrc
fi

# User specific environment
if ! [[ "$PATH" =~ "$HOME/.local/bin:$HOME/bin:" ]]
then
    PATH="$HOME/.local/bin:$HOME/bin:$PATH"
fi
export PATH

parse_git_branch() {
     git branch 2> /dev/null | sed -e '/^[^*]/d' -e 's/* \(.*\)/(\1)/'
}
export PS1="\u@\h \[\e[32m\]\w \[\e[91m\]\$(parse_git_branch)\[\e[00m\]$ "

# User specific aliases and functions
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'

# tabris venv - 프로젝트 디렉토리에서만 활성화
if [[ "$PWD" == /opt/tabris* ]] && [ -d "/opt/tabris/venv/bin" ]; then
    VENV_BIN="/opt/tabris/venv/bin"
    case ":$PATH:" in
        *":$VENV_BIN:"*) ;;
        *) PATH="$VENV_BIN:$PATH" ;;
    esac
    export PATH
fi

# 시작 폴더를 /opt/tabris로 설정
cd /opt/tabris 2>/dev/null || true
