set -e
set -x

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
    rvm get head
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
          python -m pip install -U enum34 futures
          ;;
      *) ;;
    esac
}


flake8_extras() {
    python -m pip install -U flake8
}


test_extras() {
    python -m pip install -U mock pytest pytest-cov pytest-timeout radon \
                             responses anaconda-client nbformat
}


miniconda_install() {
    curl -L http://repo.continuum.io/miniconda/Miniconda3-4.0.5-Linux-x86_64.sh -o ~/miniconda.sh
    bash ~/miniconda.sh -bfp ~/miniconda
    export PATH=~/miniconda/bin:$PATH
    hash -r
    which -a conda
    conda info
    conda install -y -q pip
    which -a pip
    which -a python
    conda config --set auto_update_conda false
}


conda_build_install() {
    # install conda
    python setup.py install
    conda info

    # install conda-build test dependencies
    conda install -y -q pytest pytest-cov pytest-timeout mock
    python -m pip install pytest-capturelog
    conda install -y -q anaconda-client numpy
    conda install -y -q -c conda-forge perl pytest-xdist
    conda config --set add_pip_as_python_dependency true

    # install conda-build runtime dependencies
    conda install -y -q filelock jinja2 patchelf conda-verify setuptools

    # install conda-build
    git clone -b $CONDA_BUILD --single-branch --depth 1000 https://github.com/conda/conda-build.git
    pushd conda-build
    python setup.py install
    conda info
    popd

    git clone https://github.com/conda/conda_build_test_recipe.git
}


if [[ $FLAKE8 == true ]]; then
    main_install
    flake8_extras
elif [[ -n $CONDA_BUILD ]]; then
    miniconda_install
    conda_build_install
else
    main_install
    test_extras
fi
