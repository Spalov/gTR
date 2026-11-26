# Windows x64 build

Each new beta increments BETA_NUMBER in gtr/__init__.py once. A rebuild uses the same number. The app label, archive names, Windows launcher metadata and Python distribution version derive from it.

Use Windows x64, CPython 3.14.10, CMake 3.25 or newer, Visual Studio 2022 Build Tools with C++ and Qt 6.12.0 MSVC2022 x64 with Qt Multimedia. The Python dependencies are pinned in `requirements-win64.txt`. The native shell and launcher do not require .NET.

Use --archives-only for public releases. Publish the complete Windows ZIP with its original filename as a binary asset on the GitHub Release for tag beta-N, using the number set in gtr/__init__.py. The updater verifies the asset's SHA-256 digest via the GitHub Releases API. Maintainers of a fork can set their release repository in gtr/updates.py.

~~~powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-win64.txt
.\.venv\Scripts\python.exe -m ruff check --isolated gtr tests packaging
.\.venv\Scripts\python.exe -m pytest --tb=short
.\.venv\Scripts\python.exe packaging\build_release.py --archives-only --qt-sdk C:\Qt\6.12.0\msvc2022_64
.\.venv\Scripts\python.exe packaging\audit_beta6.py
.\.venv\Scripts\python.exe packaging\smoke_release.py --pytest
Get-FileHash -Algorithm SHA256 release\gTR-Beta-6-Windows-x64.zip
Get-FileHash -Algorithm SHA256 release\gTR-Beta-6-source.zip
~~~

The builder verifies the embedded runtime hash, pinned wheel versions and matching Qt SDK release. Use `--python-embed PATH` with a verified cached archive for an offline build. The build writes both ZIPs and their checksums under `release`. ZIP order and timestamps are normalized. MSVC uses reproducible link settings. A different compiler, dependency binary, zlib version, source line ending or Authenticode timestamp can change the final bytes. Compare checksums after a rebuild on the same toolchain to establish equality.

The source archive includes Qt source, tests, synthetic fixtures and CI configuration. The smoke script checks CRCs, matching application bytes, launch entry points, and side-by-side installation from the finished ZIP using a locally simulated official digest. Use Windows with a graphical session for the launch checks. With `--allow-blocked-native-test` a local archive can be built when Windows Application Control specifically denies the test executable. Treat the native run as pending in that case. CI runs the offline suite and CTest.

## Authenticode

The build accepts `--sign-tool`, `--certificate-thumbprint` and an HTTPS `--timestamp-url`. With a publisher certificate the build signs the launcher and shell, then verifies Authenticode and RFC 3161 timestamps before writing the ZIP. Generate checksums from the final signed artifacts. [SignTool reference](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) · [Authenticode timestamping](https://learn.microsoft.com/en-us/windows/win32/seccrypto/time-stamping-authenticode-signatures)

Windows protection and organizational policies decide whether an executable can run. Publisher trust can help Smart App Control evaluate the launcher. SmartScreen can also use reputation. The supplied release uses the unsigned build and retains its second entry point. [Smart App Control signing](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control)
