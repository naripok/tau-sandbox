# opencode Agent Isolation Environment shell configuration

export PS1='[\u@opencode-sandbox \W]\$ '
alias ls='ls --color=auto'

# Persistent volume paths — tools installed in the sandbox survive across runs.
export PATH="$HOME/.local/bin:$PATH"
export PYTHONUSERBASE="$HOME/.local"
export NPM_CONFIG_PREFIX="$HOME/.local"
export PIP_USER=1
