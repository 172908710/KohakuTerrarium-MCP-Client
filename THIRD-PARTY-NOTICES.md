# Third-party components

This private portable build includes third-party software without changing its upstream license. A complete installed package/version inventory is in `runtime/bundle-manifest.json`; the runtime dependency snapshot is `requirements-runtime.lock.txt`.

License files shipped by each dependency are preserved inside `runtime/Lib/site-packages`, including the corresponding `.dist-info` directories. This notice is not a replacement for those licenses or a claim that all bundled components use the same license.

- CPython: `runtime/LICENSE.txt`; bundled Tcl/Tk license files remain under `runtime/tcl/`.
- KohakuTerrarium 2.1.5: `runtime/Lib/site-packages/kohakuterrarium-2.1.5.dist-info/licenses/LICENSE` (metadata: `LicenseRef-KohakuTerrarium-1.0`). Upstream: https://github.com/Kohaku-Lab/KohakuTerrarium
- Win32-OpenSSH client: `tools/openssh/LICENSE.txt`; exact release, download source and verified archive digest are in `tools/openssh/source.json`. The bundled release is named `10.0.0.0p2-Preview`. Upstream: https://github.com/PowerShell/Win32-OpenSSH
- PyMuPDF: `runtime/Lib/site-packages/pymupdf-2.0.dist-info/COPYING`. Its published metadata states dual licensing under GNU AGPL 3.0 or an Artifex commercial license.

No upstream license is removed or replaced by the packaging script. Review the applicable component licenses before public redistribution or commercial distribution; this local build is not a license clearance report.

The bundle excludes live per-device endpoint credentials, SSH private keys, production Bridge configuration and enrollment tokens. Test fixtures remain synthetic test data.
