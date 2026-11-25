# Windows x64 build

Each new beta increments BETA_NUMBER in gtr/__init__.py once. A rebuild uses the same number. The app label, archive names, Windows launcher metadata and Python distribution version derive from it.

Use Windows x64, CPython 3.14.10, Visual Studio Build Tools with Roslyn and the .NET Framework 4.8 targeting pack. The release toolchain used Roslyn 4.14.0-3.26229.7 and the dependencies pinned in `requirements-win64.txt`.

Use --archives-only for public releases. Publish the complete Windows ZIP with its original filename as a binary asset on the GitHub Release for tag beta-N, using the number set in gtr/__init__.py. The updater verifies the asset's SHA-256 digest via the GitHub Releases API. Maintainers of a fork can set their release repository in gtr/updates.py.

~~~powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-win64.txt
.\.venv\Scripts\python.exe -m ruff check --isolated gtr tests packaging
.\.venv\Scripts\python.exe -m pytest --tb=short
.\.venv\Scripts\python.exe packaging\build_release.py --archives-only
.\.venv\Scripts\python.exe packaging\smoke_release.py --pytest
Get-FileHash -Algorithm SHA256 release\gTR-Beta-5-Windows-x64.zip
Get-FileHash -Algorithm SHA256 release\gTR-Beta-5-source.zip
~~~

The builder checks the official embedded runtime hash and the wheel versions. For an offline build, supply the cached official archive via `--python-embed PATH`. Use `--compiler PATH` to select the same Roslyn compiler explicitly. Both ZIPs and their checksums appear under `release`. Fixed ordering and normalized ZIP headers make builds repeatable from the same input bytes and toolchain. The native launcher is compiled in deterministic mode. A different compiler, dependency build, zlib version, source line encoding or signature can change the result.

The source archive includes tests, synthetic fixtures and CI configuration. The smoke script checks archive membership, CRCs, matching application sources, portable Python, both Windows entry points, and side-by-side installation from the finished ZIP using a simulated GitHub digest. Run the native startup check in an interactive Windows session. Windows CI runs the offline suite and packages the artifacts.

## Authenticode

The build accepts an optional `--sign-tool` path, `--certificate-thumbprint` and HTTPS `--timestamp-url`. Use a publisher certificate available in the current user's Windows certificate store. The build signs the launcher with SHA-256 and an RFC 3161 timestamp, then verifies the Authenticode policy and timestamp before writing the ZIP. The timestamp and signature change the final bytes, so publish fresh checksums for each signed build. [SignTool reference](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) · [Authenticode timestamping](https://learn.microsoft.com/en-us/windows/win32/seccrypto/time-stamping-authenticode-signatures)

Windows protection and organizational policies decide whether an executable can run. Publisher trust can help Smart App Control evaluate the launcher. SmartScreen can also use reputation. The supplied release uses the unsigned build and retains its second entry point. [Smart App Control signing](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control)
