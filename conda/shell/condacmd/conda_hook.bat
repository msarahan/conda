@IF DEFINED CONDA_SHLVL GOTO :EOF

@FOR %%F in ("%~dp0") do @SET __condacmd_dir=%%~dpF
@SET "PATH=%__condacmd_dir%;%PATH%"
@SET CONDA_BAT="%__condacmd_dir%conda.bat"
@SET __condacmd_dir=%__condacmd_dir:~0,-1%
@FOR %%F in ("%__condacmd_dir%") do @SET __conda_root=%%~dpF
@SET CONDA_EXE="%__conda_root%Scripts\conda.exe"
@SET __condacmd_dir=
@SET __conda_root=

@SET CONDA_SHLVL=0
