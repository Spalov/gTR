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

Both Windows builders accept `--sign-tool`, `--certificate-thumbprint` and an RFC 3161 `--timestamp-url`. Use the timestamp URL published by the provider and supported by the installed SignTool. First build the portable ZIP with `packaging/build_release.py`. It signs and verifies `gTR.exe` and the embedded `gTR-shell.exe` before packaging. Then pass that ZIP to `packaging/gassets_release.py --portable <zip>` with the same signing options. It verifies both signatures in the portable ZIP before packing the GSU and signs gAssets. Inno Setup applies Authenticode to the installer, uninstaller and temporary installer copies. The checksum file is generated after signing. [SignTool reference](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) · [Inno Setup signing](https://jrsoftware.org/inno-setup/documentation/topic_setup_signtool.htm)

Use a publisher certificate issued by a provider trusted by Smart App Control. The certificate thumbprint selects the credential from the Windows certificate store. The GSU signature is verified separately by gAssets. Verify the signed package by launching it under the intended Windows policy. [Smart App Control signing](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control)

For a signed build, the builder signs the native test executables before CTest. It then checks the actual Windows launcher and background engine using the newly built ZIP and a temporary profile. The ZIP is copied to the output directory after the launch check passes. Set `--output-dir <directory>` to keep a repair build separate from earlier artifacts. `packaging/test_gassets.py` also accepts the signing options for its test executable.

The public Windows bundle requires signing. The gAssets builder starts the signed center with the exact new GSU and verifies an isolated installation before building the combined `gTR-Beta-N-Setup-Windows-x64.exe`. This installer includes both programs. Use `--test` to build a local test kit with an isolated trust key.
