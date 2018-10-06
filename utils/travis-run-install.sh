osx_setup() {
    brew update || brew update

    brew outdated openssl || brew upgrade openssl
    brew install zsh

    # install pyenv
    git clone https://github.com/yyuu/pyenv.git ~/.pyenv
    PYENV_ROOT="$HOME/.pyenv"
    PATH="$PYENV_ROOT/bin:$PATH"
    eval "$(pyenv init -)"

    case "$PYTHON_VERSION" in
        '2.7')
            curl -O https://bootstrap.pypa.io/get-pip.py
            python get-pip.py --user
            ;;
        '3.4')
            pyenv install 3.4.4
            pyenv global 3.4.4
            ;;
        '3.5')
            pyenv install 3.5.1
            pyenv global 3.5.1
            ;;
    esac
    pyenv rehash
    export PYTHON_EXE="$(pyenv which python)"
}


main_install() {
    case "$(uname -s)" in
        'Darwin') osx_setup ;;
        'Linux') export PYTHON_EXE="$(which python)" ;;
        *) ;;
    esac

    python -m pip install psutil ruamel.yaml pycosat pycrypto
    case "${TRAVIS_PYTHON_VERSION:-PYTHON_VERSION}" in
      '2.7')
          $PYTHON_EXE -m pip install -U enum34 futures
          ;;
      *) ;;
    esac
}


flake8_extras() {
    $PYTHON_EXE -m pip install -U flake8
}


test_extras() {
    $PYTHON_EXE -m pip install -U mock pytest pytest-cov pytest-timeout radon responses
}


main_install

if [[ $FLAKE8 == true ]]; then
    flake8_extras
else
    test_extras
fi
